# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import getpass
import logging
import os

from six.moves import socketserver
import threading

from pifpaf import drivers
from pifpaf.drivers import memcached

LOG = logging.getLogger(__name__)


class SyslogServerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        LOG.debug("swift output: %s", self.request[0].strip())


class SyslogServer(socketserver.ThreadingMixIn,
                   socketserver.UnixDatagramServer):
    pass


class FakeSyslog(threading.Thread):
    def __init__(self, server_address):
        super(FakeSyslog, self).__init__()
        self.daemon = True
        self.server = SyslogServer(server_address, SyslogServerHandler)
        self.run = self.server.serve_forever
        self.stop = self.server.shutdown


class SwiftDriver(drivers.Driver):

    DEFAULT_PORT = 8080
    DEFAULT_PORT_ACCOUNT = 5060
    DEFAULT_PORT_CONTAINER = 5061
    DEFAULT_PORT_OBJECT = 5062
    DEFAULT_PORT_MEMCACHED = 5063

    def __init__(self, port=DEFAULT_PORT, account_port=DEFAULT_PORT_ACCOUNT,
                 container_port=DEFAULT_PORT_CONTAINER,
                 object_port=DEFAULT_PORT_OBJECT,
                 memcached_port=DEFAULT_PORT_MEMCACHED, **kwargs):
        super(SwiftDriver, self).__init__(templatedir="swift",
                                          **kwargs)
        self.port = port
        self.account_port = account_port
        self.container_port = container_port
        self.object_port = object_port
        self.memcached_port = memcached_port

    @classmethod
    def get_parser(cls, parser):
        parser.add_argument("--port",
                            type=int,
                            default=cls.DEFAULT_PORT,
                            help="port to use for Swift Proxy Server")
        parser.add_argument("--object-port",
                            type=int,
                            default=cls.DEFAULT_PORT_OBJECT,
                            help="port to use for Swift Object Server")
        parser.add_argument("--container-port",
                            type=int,
                            default=cls.DEFAULT_PORT_CONTAINER,
                            help="port to use for Swift Container Server")
        parser.add_argument("--account-port",
                            type=int,
                            default=cls.DEFAULT_PORT_ACCOUNT,
                            help="port to use for Swift Account Server")
        parser.add_argument("--memcached-port",
                            type=int,
                            default=cls.DEFAULT_PORT_MEMCACHED,
                            help="port to use for memcached Server")
        return parser

    def _setUp(self):
        super(SwiftDriver, self)._setUp()

        self._ensure_xattr_support()

        if LOG.isEnabledFor(logging.DEBUG):
            s = FakeSyslog(os.path.join(self.tempdir, "log"))
            s.start()
            self.addCleanup(s.stop)

        template_env = {
            "TMP_DIR": self.tempdir,
            "PORT": self.port,
            "ACCOUNT_PORT": self.account_port,
            "CONTAINER_PORT": self.container_port,
            "OBJECT_PORT": self.object_port,
            "MEMCACHED_PORT": self.memcached_port,
            "USER": getpass.getuser(),
        }

        for name in ["swift.conf", "proxy.conf", "account.conf",
                     "container.conf", "object.conf",
                     "container-sync-realms.conf",
                     "sitecustomize.py"]:
            self.template(name, template_env, os.path.join(self.tempdir, name))

        self.useFixture(memcached.MemcachedDriver(self.memcached_port))
        for name in ["object", "container", "account"]:
            path = os.path.join(self.tempdir, "%s.builder" % name)
            port = getattr(self, "%s_port" % name)
            self._exec(["swift-ring-builder", path, "create", "10", "1", "1"])
            self._exec(["swift-ring-builder", path, "add",
                        "r1z1-127.0.0.1:%s/fakedisk" % port, "1"])
            self._exec(["swift-ring-builder", path, "rebalance"])

        # NOTE(sileht): to use sitecustomize.py that monkeypatch swift to be
        # able to start it in a virtualenv
        env = {'PYTHONPATH': self.tempdir}

        for name in ["object", "container", "account", "proxy"]:
            c, _ = self._exec(["swift-%s-server" % name,
                               os.path.join(self.tempdir, "%s.conf" % name)],
                              env=env, wait_for_line="started")
            self.addCleanup(self._kill, c)

        # NOTE(sileht): we have no log, so ensure it work before returning
        # swiftclient retries 3 times before give up
        testfile = os.path.join(self.tempdir, "pifpaf_test_file")
        self._touch(testfile)
        self._exec(["swift", "-A", "http://localhost:8080/auth/v1.0",
                    "-U", "test:tester", "-K", "testing", "stat", "-v"])
        self._exec(["swift", "-A", "http://localhost:8080/auth/v1.0",
                    "-U", "test:tester", "-K", "testing", "upload", "-v",
                    "pifpaf", testfile])

        self.putenv("SWIFT_PORT", str(self.port))
        self.putenv("SWIFT_USERNAME", "test:tester")
        self.putenv("SWIFT_PASSWORD", "testing")
        self.putenv("SWIFT_AUTH_URL", "http://localhost:%d/auth/v1.0" %
                    self.port)
        self.putenv("SWIFT_URL",
                    "swift://test%%3Atester:testing@localhost:%d/auth/v1.0" %
                    self.port)
        self.putenv("URL",
                    "swift://test%%3Atester:testing@localhost:%d/auth/v1.0" %
                    self.port)
