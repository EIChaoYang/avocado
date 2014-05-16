# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2013-2014
# Author: Lucas Meneghel Rodrigues <lmr@redhat.com>

"""
Class that describes a sequence of automated operations.
"""
import imp
import logging
import os
import sys
import traceback
import uuid

from avocado.core import data_dir
from avocado.core import output
from avocado.core import status
from avocado.core import exceptions
from avocado.core import error_codes
from avocado import multiplex_config
from avocado import test
from avocado import result

_NEW_ISSUE_LINK = 'https://github.com/avocado-framework/avocado/issues/new'


class Job(object):

    """
    A Job is a set of operations performed on a test machine.

    Most of the time, we are interested in simply running tests,
    along with setup operations and event recording.
    """

    def __init__(self, args=None):
        """
        Creates a Job instance.

        :param args: an instance of :class:`argparse.Namespace`.
        """

        self.args = args
        if args is not None:
            self.unique_id = args.unique_id or str(uuid.uuid4())
        else:
            self.unique_id = str(uuid.uuid4())
        self.debugdir = data_dir.get_job_logs_dir(self.args)
        self.debuglog = os.path.join(self.debugdir, "debug.log")
        if self.args is not None:
            self.loglevel = args.log_level or logging.DEBUG
            self.multiplex_file = args.multiplex_file
        else:
            self.loglevel = logging.DEBUG
            self.multiplex_file = None
        self.test_dir = data_dir.get_test_dir()
        self.test_index = 1
        self.status = "RUNNING"

        self.output_manager = output.OutputManager()

    def _load_test_instance(self, params):
        """
        Find the test url from the first component of the test shortname, and load the url.

        :param params: Dictionary with test params.
        """
        shortname = params.get('shortname')
        url = shortname.split('.')[0]
        path_attempt = os.path.abspath(url)
        if os.path.exists(path_attempt):
            test_class = test.DropinTest
            test_instance = test_class(path=path_attempt,
                                       base_logdir=self.debugdir,
                                       job=self)
        else:
            try:
                test_module_dir = os.path.join(self.test_dir, url)
                f, p, d = imp.find_module(url, [test_module_dir])
                test_module = imp.load_module(url, f, p, d)
                f.close()
                test_class = getattr(test_module, url)
            except ImportError:
                test_class = test.MissingTest
            finally:
                test_instance = test_class(name=url,
                                           base_logdir=self.debugdir,
                                           params=params,
                                           job=self)
        return test_instance

    def run_test(self, params):
        """
        Run a single test.

        :param params: Dictionary with test params.
        :type params: dict
        :return: an instance of :class:`avocado.test.Test`.
        """
        test_instance = self._load_test_instance(params)
        test_instance.run_avocado()
        return test_instance

    def test_runner(self, params_list, test_result):
        """
        Run one or more tests and report with test result.

        :param params_list: a list of param dicts.
        :param test_result: An instance of :class:`avocado.result.TestResult`.
        :return: a list of test failures.
        """
        failures = []
        test_result.start_tests()
        for params in params_list:
            test_instance = self.run_test(params)
            test_result.check_test(test_instance)
            if not status.mapping[test_instance.status]:
                failures.append(test_instance.name)
        test_result.end_tests()
        return failures

    def _make_test_runner(self):
        if hasattr(self.args, 'test_runner'):
            test_runner = self.args.test_runner
        else:
            test_runner = self.test_runner
        return test_runner

    def _make_test_result(self, urls):
        if hasattr(self.args, 'test_result'):
            test_result_class = self.args.test_result
        else:
            test_result_class = result.HumanTestResult
        if self.args is not None:
            self.args.test_result_debuglog = self.debuglog
            self.args.test_result_loglevel = self.loglevel
            self.args.test_result_total = len(urls)
        test_result = test_result_class(self.output_manager, self.args)
        return test_result

    def _run(self, urls=None, multiplex_file=None):
        """
        Unhandled job method. Runs a list of test URLs to its completion.

        :param urls: String with tests to run.
        :param multiplex_file: File that multiplexes a given test url.

        :return: Integer with overall job status. See
                 :mod:`avocado.core.error_codes` for more information.
        :raise: Any exception (avocado crashed), or
                :class:`avocado.core.exceptions.JobBaseException` errors,
                that configure a job failure.
        """
        params_list = []
        if urls is None:
            if self.args and self.args.url is not None:
                urls = self.args.url.split()
        else:
            if isinstance(urls, str):
                urls = urls.split()

        if urls is not None:
            for url in urls:
                params_list.append({'shortname': url})

        if multiplex_file is None:
            if self.args and self.args.multiplex_file is not None:
                multiplex_file = os.path.abspath(self.args.multiplex_file)
        else:
            multiplex_file = os.path.abspath(multiplex_file)

        if multiplex_file is not None:
            params_list = []
            if urls is not None:
                for url in urls:
                    parser = multiplex_config.Parser(multiplex_file)
                    parser.only_filter(url)
                    dcts = [d for d in parser.get_dicts()]
                    if dcts:
                        for dct in dcts:
                            params_list.append(dct)
                    else:
                        params_list.append({'shortname': url})
            else:
                parser = multiplex_config.Parser(multiplex_file)
                for dct in parser.get_dicts():
                    params_list.append(dct)

        test_result = self._make_test_result(params_list)
        test_runner = self._make_test_runner()

        failures = test_runner(params_list, test_result)
        # If it's all good so far, set job status to 'PASS'
        if self.status == 'RUNNING':
            self.status = 'PASS'
        # Let's clean up test artifacts
        if self.args is not None and not self.args.keep_tmp_files:
            data_dir.clean_tmp_files()
        tests_status = not bool(failures)
        if tests_status:
            return error_codes.numeric_status['AVOCADO_ALL_OK']
        else:
            return error_codes.numeric_status['AVOCADO_TESTS_FAIL']

    def run(self, urls=None, multiplex_file=None):
        """
        Handled main job method. Runs a list of test URLs to its completion.

        Note that the behavior is as follows:
        * If urls is provided alone, just make a simple list with no specific params (all tests use default params).
        * If urls and multiplex_file are provided, multiplex provides params and variants to all tests it can.
        * If multiplex_file is provided alone, just use the matrix produced by the file

        The test runner figures out which tests need to be run on an empty urls list by assuming the first component
        of the shortname is the test url.

        :param urls: String with tests to run.
        :param multiplex_file: File that multiplexes a given test url.

        :return: Integer with overall job status. See
                 :mod:`avocado.core.error_codes` for more information.
        """
        try:
            return self._run(urls, multiplex_file)
        except exceptions.JobBaseException, details:
            self.status = details.status
            fail_class = details.__class__.__name__
            self.output_manager.log_fail_header('Avocado job failed: %s: %s' %
                                                (fail_class, details))
            return error_codes.numeric_status['AVOCADO_JOB_FAIL']
        except Exception, details:
            self.status = "ERROR"
            exc_type, exc_value, exc_traceback = sys.exc_info()
            tb_info = traceback.format_exception(exc_type, exc_value,
                                                 exc_traceback.tb_next)
            fail_class = details.__class__.__name__
            self.output_manager.log_fail_header('Avocado crashed: %s: %s' %
                                                (fail_class, details))
            for line in tb_info:
                self.output_manager.error(line)
            self.output_manager.log_fail_header('Please include the traceback '
                                                'info and command line used on '
                                                'your bug report')
            self.output_manager.log_fail_header('Report bugs visiting %s' %
                                                _NEW_ISSUE_LINK)
            return error_codes.numeric_status['AVOCADO_CRASH']


class TestModuleRunner(object):

    """
    Convenience class to make avocado test modules executable.
    """

    def __init__(self, module='__main__'):
        if isinstance(module, basestring):
            self.module = __import__(module)
            for part in module.split('.')[1:]:
                self.module = getattr(self.module, part)
        else:
            self.module = module
        self.url = None
        for key, value in self.module.__dict__.iteritems():
            try:
                if issubclass(value, test.Test):
                    self.url = key
            except TypeError:
                pass
        self.job = Job()
        if self.url is not None:
            sys.exit(self.job.run(urls=[self.url]))
        sys.exit(error_codes.numeric_status['AVOCADO_ALL_OK'])

main = TestModuleRunner
