import logging
import os
import datetime

import sys

from ansible_bender.builder import get_builder
from ansible_bender.builders.base import BuildState
from ansible_bender.constants import OUT_LOGGER, OUT_LOGGER_FORMAT
from ansible_bender.core import AnsibleRunner
from ansible_bender.db import Database
from ansible_bender.exceptions import AbBuildUnsuccesful
from ansible_bender.utils import set_logging

out_logger = logging.getLogger(OUT_LOGGER)


class Application:
    def __init__(self, debug=False, db_path=None, verbose=False, init_logging=True):
        """
        :param debug: bool, provide debug output if True
        :param db_path: str, path to json file where the database stores the data persistently
        :param verbose: bool, print verbose output
        :param init_logging: bool, set up logging if True
        """
        if init_logging:
            self.set_logging(debug=debug, verbose=verbose)
        self.verbose = verbose
        self.debug = debug
        self.db = Database(db_path=db_path)
        self.db_path = self.db.db_root_path

    @staticmethod
    def set_logging(debug=False, verbose=False):
        """ configure logging """
        if debug:
            set_logging(level=logging.DEBUG)
        elif verbose:
            set_logging(level=logging.INFO)
            set_logging(logger_name=OUT_LOGGER, level=logging.INFO, format=OUT_LOGGER_FORMAT,
                        handler_kwargs={"stream": sys.stdout})
        else:
            set_logging(level=logging.WARNING)
            set_logging(logger_name=OUT_LOGGER, level=logging.INFO, format=OUT_LOGGER_FORMAT,
                        handler_kwargs={"stream": sys.stdout})

    def build(self, playbook_path, build, build_volumes=None):
        """
        build container image

        :param playbook_path: str, path to playbook
        :param build: instance of Build
        :param build_volumes: list of str, bind-mount specification: ["/host:/cont", ...]
        """
        if not os.path.isfile(playbook_path):
            raise RuntimeError("No such file or directory: %s" % playbook_path)

        build.debug = self.debug
        build.verbose = self.verbose
        # we have to record as soon as possible
        self.db.record_build(build)

        builder = self.get_builder(build)
        # let's record base image as a first layer
        base_image_id = builder.get_image_id(build.base_image)
        build.record_layer(None, base_image_id, None, cached=True)

        a_runner = AnsibleRunner(playbook_path, builder, build, debug=self.debug)

        build.build_start_time = datetime.datetime.now()
        self.db.record_build(build, build_state=BuildState.IN_PROGRESS)

        if not builder.is_base_image_present():
            builder.pull()
        py_intrprtr = builder.find_python_interpreter()

        builder.create(build_volumes=build_volumes)

        try:
            try:
                output = a_runner.build(self.db_path, python_interpreter=py_intrprtr)
            except AbBuildUnsuccesful as ex:
                b = self.db.record_build(None, build_id=build.build_id, build_state=BuildState.FAILED,
                                         set_finish_time=True)
                b.log_lines = ex.output.split("\n")
                self.db.record_build(b)
                # TODO: let this be done by the callback plugin
                image_name = build.target_image + "-failed"
                builder.commit(image_name)
                out_logger.info("Image build failed /o\\")
                out_logger.info("The progress is saved into image '%s'", image_name)
                raise

            b = self.db.record_build(None, build_id=build.build_id, build_state=BuildState.DONE,
                                     set_finish_time=True)
            b.log_lines = output
            self.db.record_build(b)
            builder.commit(build.target_image)
            out_logger.info("Image '%s' was built successfully \o/",  build.target_image)
        finally:
            builder.clean()

    def get_build(self, build_id=None):
        """
        get selected build or latest build if build_id is None

        :param build_id: str or None
        :return: build
        """
        if build_id is None:
            return self.db.get_latest_build()
        return self.db.get_build(build_id)

    def get_logs(self, build_id=None):
        """
        get logs for a specific build, if build_id is not, select the latest build

        :param build_id: str or None
        :return: list of str
        """
        build = self.get_build(build_id=build_id)
        return build.log_lines

    def list_builds(self):
        return self.db.load_builds()

    def inspect(self, build_id=None):
        """
        provide detailed information about the selected build

        :param build_id: str or None
        :return: dict
        """
        build = self.get_build(build_id=build_id)
        di = build.to_dict()
        del di["log_lines"]  # we have a dedicated command for that
        del di["layer_index"]  # internal info
        return di

    def get_builder(self, build):
        return get_builder(build.builder_name)(build, debug=self.debug)

    def maybe_load_from_cache(self, content, build_id):
        build = self.db.get_build(build_id)
        builder = self.get_builder(build)

        if not build.cache_tasks:
            return

        base_image_id = build.get_top_layer_id()
        layer_id = self.get_layer(content, base_image_id)
        if layer_id:
            builder.swap_working_container()
        return layer_id

    def get_layer(self, content, base_image_id):
        """
        provide a layer for given content and base_image_id; if there
        is such layer in cache store, return its layer_id

        :param content:
        :param base_image_id:
        :return:
        """
        return self.db.get_cached_layer(content, base_image_id)

    def record_progress(self, build, content, layer_id, build_id=None):
        """
        record build progress to the database

        :param build:
        :param content:
        :param layer_id:
        :param build_id:
        :return:
        """
        if build_id:
            build = self.db.get_build(build_id)
        base_image_id = build.get_top_layer_id()
        was_cached = False
        if not layer_id:
            # skipped task, it was cached
            layer_id = self.get_layer(content, base_image_id)
            was_cached = True
        build.record_layer(content, layer_id, base_image_id, cached=was_cached)
        self.db.record_build(build)
        return base_image_id

    def cache_task_result(self, content, build_id):
        """ snapshot the container after a task was executed """
        build = self.db.get_build(build_id)
        if not build.cache_tasks:  # actually we could still cache results
            return
        timestamp = datetime.datetime.now().strftime("%Y%M%d-%H%M%S")
        image_name = "%s-%s" % (build.target_image, timestamp)
        # buildah doesn't accept upper case
        image_name = image_name.lower()
        builder = self.get_builder(build)
        # FIXME: do not commit metadata, just filesystem
        layer_id = builder.commit(image_name)
        base_image_id = self.record_progress(build, content, layer_id)
        self.db.save_layer(layer_id, base_image_id, content)
        return image_name

    def clean(self):
        self.db.release()
