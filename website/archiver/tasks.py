import requests
import json

import celery
from celery.utils.log import get_task_logger

from framework.tasks import app as celery_app
from framework.tasks.utils import logged
from framework.exceptions import HTTPError


from website.archiver import (
    ARCHIVER_PENDING,
    ARCHIVER_CHECKING,
    ARCHIVER_SUCCESS,
    ARCHIVER_FAILURE,
    ARCHIVER_SENDING,
    ARCHIVER_SENT,
    ARCHIVER_SIZE_EXCEEDED,
    ARCHIVER_NETWORK_ERROR,
    ARCHIVER_UNCAUGHT_ERROR,
    AggregateStatResult,
)
from website.archiver import utils
from website.archiver.model import ArchiveJob

from website.project import signals as project_signals
from website import settings
from website.app import init_addons, do_set_backends

def create_app_context():
    try:
        init_addons(settings)
        do_set_backends(settings)
    except AssertionError:  # ignore AssertionErrors
        pass


logger = get_task_logger(__name__)

class ArchiverTask(celery.Task):
    abstract = True
    max_retries = 0

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if not kwargs:
            return
        job = ArchiveJob.load(kwargs['job_pk'])
        src, dst, user = job.info()
        if isinstance(exc, ArchiverSizeExceeded):
            dst.archive_status = ARCHIVER_SIZE_EXCEEDED
            dst.save()
            utils.handle_archive_fail(
                ARCHIVER_SIZE_EXCEEDED,
                src, dst, user,
                exc.result
            )
        elif isinstance(exc, HTTPError):
            dst.archive_status = ARCHIVER_NETWORK_ERROR
            dst.save()
            utils.handle_archive_fail(
                ARCHIVER_NETWORK_ERROR,
                src, dst, user,
                dst.archived_providers
            )
        else:
            dst.archive_status = ARCHIVER_UNCAUGHT_ERROR
            dst.save()
            utils.handle_archive_fail(
                ARCHIVER_UNCAUGHT_ERROR,
                src, dst, user,
                [einfo]
            )
        raise exc

class ArchiverSizeExceeded(Exception):

    def __init__(self, result, *args, **kwargs):
        super(ArchiverSizeExceeded, self).__init__(*args, **kwargs)
        self.result = result

@celery_app.task(base=ArchiverTask, name="archiver.stat_addon")
@logged('stat_addon')
def stat_addon(addon_short_name, job_pk):
    """Collect metadata about the file tree of a given addon

    :param addon_short_name: AddonConfig.short_name of the addon to be examined
    :param src_pk: primary key of node being registered
    :param dst_pk: primary key of registration node
    :param user_pk: primary key of registration initatior
    :return: AggregateStatResult containing file tree metadata
    """
    create_app_context()
    job = ArchiveJob.load(job_pk)
    src, dst, user = job.info()
    dst.archive_job.update_target(addon_short_name, ARCHIVER_CHECKING)
    src_addon = src.get_addon(addon_short_name)
    try:
        file_tree = src_addon._get_file_tree(user=user)
    except HTTPError as e:
        dst.archive_job.update_target(
            addon_short_name,
            ARCHIVER_NETWORK_ERROR,
            errors=[e.data['error']],
        )
        raise
    result = AggregateStatResult(
        src_addon._id,
        addon_short_name,
        targets=[utils.aggregate_file_tree_metadata(addon_short_name, file_tree, user)],
    )
    return result

@celery_app.task(base=ArchiverTask, name="archiver.make_copy_request")
@logged('make_copy_request')
def make_copy_request(job_pk, url, data):
    """Make the copy request to the WaterBulter API and handle
    successful and failed responses

    :param dst_pk: primary key of registration node
    :param url: URL to send request to
    :param data: <dict> of setting to send in POST to WaterBulter API
    :return: None
    """
    create_app_context()
    job = ArchiveJob.load(job_pk)
    src, dst, user = job.info()
    provider = data['source']['provider']
    dst.archive_job.update_target(provider, ARCHIVER_SENDING)
    logger.info("Sending copy request for addon: {0} on node: {1}".format(provider, dst._id))
    res = requests.post(url, data=json.dumps(data))
    logger.info("Copy request responded with {0} for addon: {1} on node: {2}".format(res.status_code, provider, dst._id))
    dst.archive_job.update_target(provider, ARCHIVER_SENT)
    if not res.ok:
        dst.archive_job.update_target(
            provider,
            ARCHIVER_FAILURE,
            errors=[res.json()],
        )
        raise HTTPError(res.status_code)
    elif res.status_code in (200, 201):
        dst.archive_job.update_target(provider, ARCHIVER_SUCCESS)
    project_signals.archive_callback.send(dst)

@celery_app.task(base=ArchiverTask, name="archiver.archive_addon")
@logged('archive_addon')
def archive_addon(addon_short_name, job_pk, stat_result):
    """Archive the contents of an addon by making a copy request to the
    WaterBulter API

    :param addon_short_name: AddonConfig.short_name of the addon to be archived
    :param src_pk: primary key of node being registered
    :param dst_pk: primary key of registration node
    :param user_pk: primary key of registration initatior
    :return: None
    """
    create_app_context()
    job = ArchiveJob.load(job_pk)
    src, dst, user = job.info()
    logger.info("Archiving addon: {0} on node: {1}".format(addon_short_name, src._id))
    dst.archive_job.update_target(
        addon_short_name,
        ARCHIVER_PENDING,
        stat_result=stat_result,
    )
    src_provider = src.get_addon(addon_short_name)
    folder_name = src_provider.archive_folder_name
    provider = src_provider.config.short_name
    cookie = user.get_or_create_cookie()
    data = {
        'source': {
            'cookie': cookie,
            'nid': src._id,
            'provider': provider,
            'path': '/',
        },
        'destination': {
            'cookie': cookie,
            'nid': dst._id,
            'provider': settings.ARCHIVE_PROVIDER,
            'path': '/',
        },
        'rename': folder_name,
    }
    copy_url = settings.WATERBUTLER_URL + '/ops/copy'
    make_copy_request.delay(job_pk=job_pk, url=copy_url, data=data)


@celery_app.task(base=ArchiverTask, name="archiver.archive_node")
@logged('archive_node')
def archive_node(results, job_pk):
    """First use the results of #stat_node to check disk usage of the
    initated registration, then either fail the registration or
    create a celery.group group of subtasks to archive addons

    :param results: results from the #stat_addon subtasks spawned in #stat_node
    :param src_pk: primary key of node being registered
    :param dst_pk: primary key of registration node
    :param user_pk: primary key of registration initatior
    :return: None
    """
    create_app_context()
    job = ArchiveJob.load(job_pk)
    src, dst, user = job.info()
    logger.info("Archiving node: {0}".format(src._id))
    dst.save()
    stat_result = AggregateStatResult(
        src._id,
        src.title,
        targets=results,
    )
    if stat_result.disk_usage > settings.MAX_ARCHIVE_SIZE:
        raise ArchiverSizeExceeded(result=stat_result)
    else:
        for result in stat_result.targets:
            if not result.targets:
                continue
            archive_addon.delay(
                addon_short_name=result.target_name,
                job_pk=job_pk,
                stat_result=result,
            )

@celery_app.task(bind=True, base=ArchiverTask, name='archiver.archive')
@logged('archive')
def archive(self, job_pk):
    """Saves the celery task id, and start a celery.chord
    that runs stat_addon for each addon attached to the Node,
    then runs archive node with the result

    :param src_pk: primary key of node being registered
    :param dst_pk: primary key of registration node
    :param user_pk: primary key of registration initatior
    :return: None
    """
    create_app_context()
    job = ArchiveJob.load(job_pk)
    src, dst, user = job.info()
    logger = get_task_logger(__name__)
    logger.info("Received archive task for Node: {0} into Node: {1}".format(src._id, dst._id))
    celery.chord(
        celery.group(
            stat_addon.si(
                addon_short_name=addon.config.short_name,
                job_pk=job_pk,
            )
            for addon in [
                src.get_addon(target.name)
                for target in dst.archive_job.target_addons
            ]
        )
    )(archive_node.s(job_pk))
