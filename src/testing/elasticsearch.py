# -*- coding: utf-8 -*-
#  Copyright 2013 Takeshi KOMIYA
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import re
import sys
import yaml
import socket
import signal
import urllib
import tempfile
from glob import glob
from time import sleep
from shutil import copyfile, copytree, rmtree
from datetime import datetime


__all__ = ['Elasticsearch', 'skipIfNotFound']

DEFAULT_SETTINGS = dict(auto_start=2,
                        base_dir=None,
                        elasticsearch_home=None,
                        pid=None,
                        port=None)


class Elasticsearch(object):
    def __init__(self, **kwargs):
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(kwargs)
        self.pid = None
        self._owner_pid = os.getpid()
        self._use_tmpdir = False

        if self.base_dir:
            if self.base_dir[0] != '/':
                self.settings['base_dir'] = os.path.join(os.getcwd(), self.base_dir)
        else:
            self.settings['base_dir'] = tempfile.mkdtemp()
            self._use_tmpdir = True

        if self.elasticsearch_home is None:
            self.settings['elasticsearch_home'] = find_elasticsearch_home()

        user_config = self.settings.get('elasticsearch_yaml')
        with open(os.path.join(self.elasticsearch_home, 'config', 'elasticsearch.yml')) as fd:
            self.settings['elasticsearch_yaml'] = yaml.load(fd.read()) or {}
            self.settings['elasticsearch_yaml']['path.data'] = os.path.join(self.base_dir, 'data')
            self.settings['elasticsearch_yaml']['path.logs'] = os.path.join(self.base_dir, 'logs')

            if self.port:
                self.settings['elasticsearch_yaml']['http.port'] = self.port

            if user_config:
                for key, value in user_config.items():
                    self.settings['elasticsearch_yaml'][key] = value

        if self.auto_start:
            if self.auto_start >= 2:
                self.setup()

            self.start()

    def __del__(self):
        self.stop()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()

    def __getattr__(self, name):
        if name in self.settings:
            return self.settings[name]
        else:
            raise AttributeError("'Elasticsearch' object has no attribute '%s'" % name)

    def dsn(self, **kwargs):
        return {'hosts': ['127.0.0.1:%d' % self.elasticsearch_yaml['http.port']]}

    def setup(self):
        # (re)create directory structure
        for subdir in ['data', 'logs']:
            path = os.path.join(self.base_dir, subdir)
            if not os.path.exists(path):
                os.makedirs(path)
                os.chmod(path, 0o700)

        # conf directory
        for filename in os.listdir(self.elasticsearch_home):
            srcpath = os.path.join(self.elasticsearch_home, filename)
            destpath = os.path.join(self.base_dir, filename)
            if not os.path.exists(destpath):
                if filename in ['lib', 'plugins']:
                    os.symlink(srcpath, destpath)
                elif os.path.isdir(srcpath):
                    copytree(srcpath, destpath)
                else:
                    copyfile(srcpath, destpath)

    def prestart(self):
        # assign port to elasticsearch
        self.settings['elasticsearch_yaml']['http.port'] = self.port or get_unused_port()

        # generate cassandra.yaml
        with open(os.path.join(self.base_dir, 'config', 'elasticsearch.yml'), 'wt') as fd:
            fd.write(yaml.dump(self.elasticsearch_yaml, default_flow_style=False))

    def start(self):
        if self.pid:
            return  # already started

        self.prestart()

        logger = open(os.path.join(self.base_dir, 'logs', 'elasticsearch-launch.log'), 'wt')
        pid = os.fork()
        if pid == 0:
            os.dup2(logger.fileno(), sys.__stdout__.fileno())
            os.dup2(logger.fileno(), sys.__stderr__.fileno())

            try:
                elasticsearch_bin = os.path.join(self.base_dir, 'bin', 'elasticsearch')
                os.execl(elasticsearch_bin, elasticsearch_bin)
            except Exception as exc:
                raise RuntimeError('failed to launch elasticsearch: %r' % exc)
        else:
            logger.close()

            exec_at = datetime.now()
            while True:
                if os.waitpid(pid, os.WNOHANG)[0] != 0:
                    raise RuntimeError("*** failed to launch elasticsearch ***\n" + self.read_log())

                if self.is_connection_available():
                    break

                if (datetime.now() - exec_at).seconds > 20.0:
                    print datetime.now()
                    raise RuntimeError("*** failed to launch elasticsearch (timeout) ***\n" + self.read_log())

                sleep(0.1)

            self.pid = pid

    def stop(self, _signal=signal.SIGINT):
        self.terminate(_signal)
        self.cleanup()

    def terminate(self, _signal=signal.SIGINT):
        if self.pid is None:
            return  # not started

        if self._owner_pid != os.getpid():
            return  # could not stop in child process

        try:
            os.kill(self.pid, _signal)
            killed_at = datetime.now()
            while (os.waitpid(self.pid, os.WNOHANG)):
                if (datetime.now() - killed_at).seconds > 10.0:
                    os.kill(self.pid, signal.SIGKILL)
                    raise RuntimeError("*** failed to shutdown elasticsearch (timeout) ***\n" + self.read_log())

                sleep(0.1)
        except OSError:
            pass

        self.pid = None

    def cleanup(self):
        if self.pid is not None:
            return

        if self._use_tmpdir and os.path.exists(self.base_dir):
            rmtree(self.base_dir, ignore_errors=True)
            self._use_tmpdir = False

    def read_log(self):
        try:
            with open(os.path.join(self.base_dir, 'logs', 'elasticsearch-launch.log')) as log:
                return log.read()
        except Exception as exc:
            raise RuntimeError("failed to open file:logs/elasticsearch-launch.log: %r" % exc)

    def is_connection_available(self):
        try:
            url = 'http://127.0.0.1:%d/' % self.elasticsearch_yaml['http.port']
            urllib.urlopen(url)
            return True
        except Exception:
            return False


def skipIfNotInstalled(arg=None):
    if sys.version_info < (2, 7):
        from unittest2 import skipIf
    else:
        from unittest import skipIf

    def decorator(fn, path=arg):
        if path:
            cond = not os.path.exists(path)
        else:
            try:
                find_elasticsearch_home()  # raise exception if not found
                cond = False
            except:
                cond = True  # not found

        return skipIf(cond, "Elasticsearch not found")(fn)

    if callable(arg):  # execute as simple decorator
        return decorator(arg, None)
    else:  # execute with path argument
        return decorator


skipIfNotFound = skipIfNotInstalled


def strip_version(dir):
    m = re.search('(\d+)\.(\d+)\.(\d+)', dir)
    if m is None:
        return None
    else:
        return tuple([int(ver) for ver in m.groups()])


def find_elasticsearch_home():
    elasticsearch_home = os.environ.get('ES_HOME')
    if elasticsearch_home and os.path.exists(os.path.join(elasticsearch_home, 'bin', 'elasticsearch')):
        return elasticsearch_home

    # search newest elasticsearch-x.x.x directory
    globbed = glob("/usr/local/*elasticsearch*") + glob("*elasticsearch*")
    elasticsearch_dirs = [os.path.abspath(dir) for dir in globbed if os.path.isdir(dir)]
    if elasticsearch_dirs:
        return sorted(elasticsearch_dirs, key=strip_version)[-1]

    raise RuntimeError("could not find ES_HOME")


def get_unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('localhost', 0))
    _, port = sock.getsockname()
    sock.close()

    return port