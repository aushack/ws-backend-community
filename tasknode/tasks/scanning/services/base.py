# -*- coding: utf-8 -*-
from __future__ import absolute_import

from celery import chain
from celery.utils.log import get_task_logger
from uuid import uuid4

from ....app import websight_app
from ...base import DatabaseTask, ServiceTask, NetworkServiceTask
from lib import DatetimeHelper, ConfigManager
from lib.sqlalchemy import get_network_service_scan_interval_for_organization, \
    update_network_service_scan_completed as update_network_service_scan_completed_op, \
    update_network_service_scanning_status as update_network_service_scanning_status_op, \
    check_network_service_scanning_status, create_new_network_service_scan
from .liveness import check_network_service_for_liveness
from .inspection import inspect_service_application
from .analysis import analyze_network_service_scan, create_report_for_network_service_scan
from .fingerprinting import fingerprint_network_service
from wselasticsearch.ops import update_not_network_service_scan_latest_state, update_network_service_scan_latest_state
from wselasticsearch.models import NetworkServiceLivenessModel
from .ssl import inspect_tcp_service_for_ssl_support

logger = get_task_logger(__name__)
config = ConfigManager.instance()


@websight_app.task(bind=True, base=ServiceTask)
def perform_network_service_inspection(
        self,
        org_uuid=None,
        scan_uuid=None,
        service_uuid=None,
):
    """
    Inspect the referenced network service on behalf of the referenced organization.
    :param org_uuid: The UUID of the organization to check the service on behalf of.
    :param scan_uuid: The UUID of the network service scan that this inspection run is associated
    with.
    :param service_uuid: The UUID of the network service to inspect.
    :return: None
    """
    logger.info(
        "Now inspecting network service %s for organization %s. Scan is %s."
        % (service_uuid, org_uuid, scan_uuid)
    )
    liveness_sig = check_network_service_for_liveness.si(
        org_uuid=org_uuid,
        do_fingerprinting=True,
        do_ssl_inspection=True,
        scan_uuid=scan_uuid,
        service_uuid=service_uuid,
    )
    self.finish_after(signature=liveness_sig)


@websight_app.task(bind=True, base=NetworkServiceTask)
def scan_network_service(
        self,
        org_uuid=None,
        network_service_uuid=None,
        check_liveness=True,
        liveness_cause=None,
        check_ssl_support=True,
        inspect_applications=True
):
    """
    Scan the given network service for all network service relevant data supported by Web Sight.
    :param org_uuid: The UUID of the organization that owns the network service.
    :param network_service_uuid: The UUID of the network service to scan.
    :param check_liveness: Whether or not to check if the network service is alive.
    :param liveness_cause: The reason that this network service task was configured to not check
    for liveness.
    :param check_ssl_support: Whether or not to investigate SSL support for the network service.
    :param inspect_applications: Whether or not to inspect any applications that are identified by
    network service fingerprinting.
    :return: None
    """
    logger.info(
        "Now scanning network service %s for organization %s."
        % (network_service_uuid, org_uuid)
    )
    should_scan = check_network_service_scanning_status(
        db_session=self.db_session,
        service_uuid=network_service_uuid,
        update_status=True,
    )
    if not should_scan:
        logger.info(
            "Should not scan network service %s. Exiting."
            % (network_service_uuid,)
        )
    network_service_scan = create_new_network_service_scan(
        network_service_uuid=network_service_uuid,
        db_session=self.db_session,
    )
    self.db_session.add(network_service_scan)
    self.db_session.commit()
    if check_liveness:
        is_alive = self.inspector.check_if_open()
        if not is_alive:
            logger.info(
                "Network service at %s is not alive."
                % (network_service_uuid,)
            )
            return
        else:
            liveness_model = NetworkServiceLivenessModel.from_database_model(
                network_service_scan,
                is_alive=True,
                liveness_cause="network service scan liveness check"
            )
            liveness_model.save(org_uuid)
    else:
        liveness_model = NetworkServiceLivenessModel.from_database_model(
            network_service_scan,
            is_alive=True,
            liveness_cause=liveness_cause,
        )
        liveness_model.save(org_uuid)
    task_sigs = []
    task_kwargs = {
        "org_uuid": org_uuid,
        "network_service_uuid": network_service_uuid,
        "network_service_scan_uuid": network_service_scan.uuid,
    }
    if check_ssl_support:
        supports_ssl = self.inspector.check_ssl_support()
        if supports_ssl:
            task_sigs.append(inspect_tcp_service_for_ssl_support.si(**task_kwargs))
    task_sigs.append(fingerprint_network_service.si(**task_kwargs))
    task_sigs.append(create_report_for_network_service_scan.si(**task_kwargs))
    task_sigs.append(update_network_service_scan_elasticsearch.si(**task_kwargs))
    task_sigs.append(update_network_service_scan_completed.si(**task_kwargs))
    scanning_status_sig = update_network_service_scanning_status.si(
        network_service_uuid=network_service_uuid,
        scanning_status=False,
    )
    task_sigs.append(scanning_status_sig)
    if inspect_applications:
        task_sigs.append(inspect_service_application.si(**task_kwargs))
    logger.info(
        "Now kicking off all necessary tasks to scan network service %s."
        % (network_service_uuid,)
    )
    canvas_sig = chain(task_sigs, link_error=scanning_status_sig)
    self.finish_after(signature=canvas_sig)


@websight_app.task(bind=True, base=ServiceTask)
def network_service_inspection_pass(self, service_uuid=None, org_uuid=None, schedule_again=True):
    """
    This task performs a single network service scan pass, doing all of the necessary things to
    check on the state of a network service.
    :param service_uuid: The UUID of the OrganizationNetworkService to monitor.
    :param org_uuid: The UUID of the organization to monitor the network service on behalf of.
    :param schedule_again: Whether or not to schedule another monitoring task.
    :return: None
    """
    logger.info(
        "Now starting pass for network service %s. Organization is %s."
        % (service_uuid, org_uuid)
    )

    # TODO check to see if the network service has been dead for the past N times and don't requeue if it has

    should_scan = check_network_service_scanning_status(db_session=self.db_session, service_uuid=service_uuid)
    if not should_scan:
        logger.info(
            "Network service %s either is already being scanned or has been scanned too recently to continue now."
            % (service_uuid,)
        )
        return
    ip_address, port, protocol = self.get_endpoint_information(service_uuid)
    network_service_scan = create_new_network_service_scan(
        network_service_uuid=service_uuid,
        db_session=self.db_session,
    )
    task_signatures = []
    task_signatures.append(perform_network_service_inspection.si(
        org_uuid=org_uuid,
        service_uuid=service_uuid,
        scan_uuid=network_service_scan.uuid,
    ))
    task_signatures.append(analyze_network_service_scan.si(
        network_service_scan_uuid=network_service_scan.uuid,
        org_uuid=org_uuid,
    ))
    task_signatures.append(update_network_service_scan_elasticsearch.si(
        network_service_scan_uuid=network_service_scan.uuid,
        org_uuid=org_uuid,
        network_service_uuid=service_uuid,
    ))
    task_signatures.append(update_network_service_scan_completed.si(
        network_service_scan_uuid=network_service_scan.uuid,
        org_uuid=org_uuid,
        network_service_uuid=service_uuid,
    ))
    task_signatures.append(inspect_service_application.si(
        org_uuid=org_uuid,
        network_service_scan_uuid=network_service_scan.uuid,
        network_service_uuid=service_uuid,
    ))
    canvas_sig = chain(task_signatures)
    canvas_sig.apply_async()
    if not config.task_network_service_monitoring_enabled:
        logger.info("Not scheduling another monitoring task as network service monitoring is disabled.")
    elif not schedule_again:
        logger.info("Not scheduling another monitoring task as schedule_again was False.")
    else:
        scan_interval = get_network_service_scan_interval_for_organization(
            org_uuid=org_uuid,
            db_session=self.db_session,
        )
        next_time = DatetimeHelper.seconds_from_now(scan_interval)
        logger.info(
            "Queueing up an additional instance of %s in %s seconds (%s). Endpoint is %s:%s (%s)."
            % (self.name, scan_interval, next_time, ip_address, port, protocol)
        )
        init_sig = network_service_inspection_pass.si(
            service_uuid=service_uuid,
            org_uuid=org_uuid,
            schedule_again=True,
        )
        init_sig.apply_async(eta=next_time)


@websight_app.task(bind=True, base=ServiceTask)
def update_network_service_scan_elasticsearch(
        self,
        org_uuid=None,
        network_service_scan_uuid=None,
        network_service_uuid=None,
):
    """
    Update Elasticsearch so that all of the Elasticsearch documents associated with the given network
    service scan are marked as the latest results for the given network service, and all of the previously
    collected documents so that they are marked as not being related to the most recent scan.
    :param org_uuid: The UUID of the organization to update Elasticsearch on behalf of.
    :param network_service_scan_uuid: The UUID of the network service scan to update results for.
    :param network_service_uuid: The UUID of the network service that was analyzed.
    :return: None
    """
    logger.info(
        "Now updating Elasticsearch for network service scan %s. Organization is %s."
        % (network_service_scan_uuid, org_uuid)
    )
    self.wait_for_es()
    update_network_service_scan_latest_state(scan_uuid=network_service_scan_uuid, org_uuid=org_uuid, latest_state=True)
    update_not_network_service_scan_latest_state(
        scan_uuid=network_service_scan_uuid,
        org_uuid=org_uuid,
        latest_state=False,
        network_service_uuid=network_service_uuid,
    )
    logger.info(
        "Elasticsearch updated to reflect that network service scan %s is the latest network service scan "
        "for network service %s and organization %s."
        % (network_service_scan_uuid, network_service_uuid, org_uuid)
    )


@websight_app.task(bind=True, base=ServiceTask)
def update_network_service_scan_completed(
        self,
        network_service_scan_uuid=None,
        org_uuid=None,
        network_service_uuid=None,
):
    """
    Update the referenced NetworkServiceScan to show that the network service scan has completed.
    :param network_service_scan_uuid: The UUID of the NetworkServiceScan to update.
    :param org_uuid: The UUID of the organization that the scan was run on behalf of.
    :param network_service_uuid: The UUID of the network service that scanning was completed for.
    :return: None
    """
    logger.info(
        "Now updating NetworkServiceScan %s to show its completed. Organization is %s. Service is %s."
        % (network_service_scan_uuid, org_uuid, network_service_uuid)
    )
    update_network_service_scan_completed_op(scan_uuid=network_service_scan_uuid, db_session=self.db_session)
    self.commit_session()
    logger.info(
        "Successfully updated NetworkServiceScan %s as completed."
        % (network_service_scan_uuid,)
    )


@websight_app.task(bind=True, base=DatabaseTask)
def update_network_service_scanning_status(self, network_service_uuid=None, scanning_status=None):
    """
    Update the current scanning status of the given network service to the given value.
    :param network_service_uuid: The UUID of the network service to update.
    :param scanning_status: The status to set the scanning status to.
    :return: None
    """
    logger.info(
        "Now updating scanning status for network service %s to %s."
        % (network_service_uuid, scanning_status)
    )
    update_network_service_scanning_status_op(
        status=scanning_status,
        service_uuid=network_service_uuid,
        db_session=self.db_session,
    )
    self.db_session.commit()
    logger.info(
        "Scanning status for network service %s successfully updated to %s."
        % (network_service_uuid, scanning_status)
    )
