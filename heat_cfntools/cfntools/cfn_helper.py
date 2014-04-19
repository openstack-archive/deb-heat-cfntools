#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Implements cfn metadata handling

Not implemented yet:
    * command line args
      - placeholders are ignored
"""
import atexit
import ConfigParser
import errno
import grp
import hashlib
import json
import logging
import os
import os.path
import pwd
try:
    import rpmUtils.miscutils as rpmutils
    import rpmUtils.updates as rpmupdates
    rpmutils_present = True
except ImportError:
    rpmutils_present = False
import re
import shutil
import subprocess
import tempfile

# Override BOTO_CONFIG, which makes boto look only at the specified
# config file, instead of the default locations
os.environ['BOTO_CONFIG'] = '/var/lib/heat-cfntools/cfn-boto-cfg'
from boto import cloudformation


LOG = logging.getLogger(__name__)


def to_boolean(b):
    val = b.lower().strip() if isinstance(b, basestring) else b
    return val in [True, 'true', 'yes', '1', 1]


def parse_creds_file(path='/etc/cfn/cfn-credentials'):
    '''Parse the cfn credentials file.

    Default location is as specified, and it is expected to contain
    exactly two keys "AWSAccessKeyId" and "AWSSecretKey)
    The two keys are returned a dict (if found)
    '''
    creds = {'AWSAccessKeyId': None, 'AWSSecretKey': None}
    for line in open(path):
        for key in creds:
            match = re.match("^%s *= *(.*)$" % key, line)
            if match:
                creds[key] = match.group(1)
    return creds


class HupConfig(object):
    def __init__(self, fp_list):
        self.config = ConfigParser.SafeConfigParser()
        for fp in fp_list:
            self.config.readfp(fp)

        self.load_main_section()

        self.hooks = []
        for s in self.config.sections():
            if s != 'main':
                self.hooks.append(Hook(
                    s,
                    self.config.get(s, 'triggers'),
                    self.config.get(s, 'path'),
                    self.config.get(s, 'runas'),
                    self.config.get(s, 'action')))

    def load_main_section(self):
        # required values
        self.stack = self.config.get('main', 'stack')
        self.credential_file = self.config.get('main', 'credential-file')
        try:
            with open(self.credential_file) as f:
                self.credentials = f.read()
        except Exception:
            raise Exception("invalid credentials file %s" %
                            self.credential_file)

        # optional values
        try:
            self.region = self.config.get('main', 'region')
        except ConfigParser.NoOptionError:
            self.region = 'nova'

        try:
            self.interval = self.config.getint('main', 'interval')
        except ConfigParser.NoOptionError:
            self.interval = 10

    def __str__(self):
        return '{stack: %s, credential_file: %s, region: %s, interval:%d}' % \
            (self.stack, self.credential_file, self.region, self.interval)

    def unique_resources_get(self):
        resources = []
        for h in self.hooks:
            r = h.resource_name_get()
            if r not in resources:
                resources.append(h.resource_name_get())
        return resources


class Hook(object):
    def __init__(self, name, triggers, path, runas, action):
        self.name = name
        self.triggers = triggers
        self.path = path
        self.runas = runas
        self.action = action

    def resource_name_get(self):
        sp = self.path.split('.')
        return sp[1]

    def event(self, ev_name, ev_object, ev_resource):
        if self.resource_name_get() == ev_resource and \
                ev_name in self.triggers:
            CommandRunner(self.action).run(user=self.runas)
        else:
            LOG.debug('event: {%s, %s, %s} did not match %s' %
                      (ev_name, ev_object, ev_resource, self.__str__()))

    def __str__(self):
        return '{%s, %s, %s, %s, %s}' % \
            (self.name,
             self.triggers,
             self.path,
             self.runas,
             self.action)


class CommandRunner(object):
    """Helper class to run a command and store the output."""

    def __init__(self, command, nextcommand=None):
        self._command = command
        self._next = nextcommand
        self._stdout = None
        self._stderr = None
        self._status = None

    def __str__(self):
        s = "CommandRunner:"
        s += "\n\tcommand: %s" % self._command
        if self._status:
            s += "\n\tstatus: %s" % self.status
        if self._stdout:
            s += "\n\tstdout: %s" % self.stdout
        if self._stderr:
            s += "\n\tstderr: %s" % self.stderr
        return s

    def run(self, user='root', cwd=None, env=None):
        """Run the Command and return the output.

        Returns:
            self
        """
        LOG.debug("Running command: %s" % self._command)
        cmd = ['su', user, '-c', self._command]
        subproc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env)
        output = subproc.communicate()

        self._status = subproc.returncode
        self._stdout = output[0]
        self._stderr = output[1]
        if self._status:
            LOG.debug("Return code of %d after executing: '%s'\n"
                      "stdout: '%s'\n"
                      "stderr: '%s'" % (self._status, cmd, self._stdout,
                                        self._stderr))
        if self._next:
            self._next.run()
        return self

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    @property
    def status(self):
        return self._status


class RpmHelper(object):

    if rpmutils_present:
        _rpm_util = rpmupdates.Updates([], [])

    @classmethod
    def compare_rpm_versions(cls, v1, v2):
        """Compare two RPM version strings.

        Arguments:
            v1 -- a version string
            v2 -- a version string

        Returns:
            0 -- the versions are equal
            1 -- v1 is greater
           -1 -- v2 is greater
        """
        if v1 and v2:
            return rpmutils.compareVerOnly(v1, v2)
        elif v1:
            return 1
        elif v2:
            return -1
        else:
            return 0

    @classmethod
    def newest_rpm_version(cls, versions):
        """Returns the highest (newest) version from a list of versions.

        Arguments:
            versions -- A list of version strings
                        e.g., ['2.0', '2.2', '2.2-1.fc16', '2.2.22-1.fc16']
        """
        if versions:
            if isinstance(versions, basestring):
                return versions
            versions = sorted(versions, rpmutils.compareVerOnly,
                              reverse=True)
            return versions[0]
        else:
            return None

    @classmethod
    def rpm_package_version(cls, pkg):
        """Returns the version of an installed RPM.

        Arguments:
            pkg -- A package name
        """
        cmd = "rpm -q --queryformat '%{VERSION}-%{RELEASE}' %s" % pkg
        command = CommandRunner(cmd).run()
        return command.stdout

    @classmethod
    def rpm_package_installed(cls, pkg):
        """Indicates whether pkg is in rpm database.

        Arguments:
            pkg -- A package name (with optional version and release spec).
                   e.g., httpd
                   e.g., httpd-2.2.22
                   e.g., httpd-2.2.22-1.fc16
        """
        command = CommandRunner("rpm -q %s" % pkg).run()
        return command.status == 0

    @classmethod
    def yum_package_available(cls, pkg):
        """Indicates whether pkg is available via yum.

        Arguments:
            pkg -- A package name (with optional version and release spec).
                   e.g., httpd
                   e.g., httpd-2.2.22
                   e.g., httpd-2.2.22-1.fc16
        """
        cmd_str = "yum -y --showduplicates list available %s" % pkg
        command = CommandRunner(cmd_str).run()
        return command.status == 0

    @classmethod
    def install(cls, packages, rpms=True):
        """Installs (or upgrades) a set of packages via RPM or via Yum.

        Arguments:
            packages -- a list of packages to install
            rpms     -- if True:
                        * use RPM to install the packages
                        * packages must be a list of URLs to retrieve RPMs
                        if False:
                        * use Yum to install packages
                        * packages is a list of:
                          - pkg name (httpd), or
                          - pkg name with version spec (httpd-2.2.22), or
                          - pkg name with version-release spec
                            (httpd-2.2.22-1.fc16)
        """
        if rpms:
            cmd = "rpm -U --force --nosignature "
            cmd += " ".join(packages)
            LOG.info("Installing packages: %s" % cmd)
        else:
            cmd = "yum -y install "
            cmd += " ".join(packages)
            LOG.info("Installing packages: %s" % cmd)
        command = CommandRunner(cmd).run()
        if command.status:
            LOG.warn("Failed to install packages: %s" % cmd)

    @classmethod
    def downgrade(cls, packages, rpms=True):
        """Downgrades a set of packages via RPM or via Yum.

        Arguments:
            packages -- a list of packages to downgrade
            rpms     -- if True:
                        * use RPM to downgrade (replace) the packages
                        * packages must be a list of URLs to retrieve the RPMs
                        if False:
                        * use Yum to downgrade packages
                        * packages is a list of:
                          - pkg name with version spec (httpd-2.2.22), or
                          - pkg name with version-release spec
                            (httpd-2.2.22-1.fc16)
        """
        if rpms:
            cls.install(packages)
        else:
            cmd = "yum -y downgrade "
            cmd += " ".join(packages)
            LOG.info("Downgrading packages: %s" % cmd)
            command = CommandRunner(cmd).run()
            if command.status:
                LOG.warn("Failed to downgrade packages: %s" % cmd)


class PackagesHandler(object):
    _packages = {}

    _package_order = ["dpkg", "rpm", "apt", "yum"]

    @staticmethod
    def _pkgsort(pkg1, pkg2):
        order = PackagesHandler._package_order
        p1_name = pkg1[0]
        p2_name = pkg2[0]
        if p1_name in order and p2_name in order:
            return cmp(order.index(p1_name), order.index(p2_name))
        elif p1_name in order:
            return -1
        elif p2_name in order:
            return 1
        else:
            return cmp(p1_name.lower(), p2_name.lower())

    def __init__(self, packages):
        self._packages = packages

    def _handle_gem_packages(self, packages):
        """very basic support for gems."""
        # TODO(asalkeld) support versions
        # -b == local & remote install
        # -y == install deps
        opts = '-b -y'
        for pkg_name, versions in packages.iteritems():
            if len(versions) > 0:
                cmd_str = 'gem install %s --version %s %s' % (opts,
                                                              versions[0],
                                                              pkg_name)
                CommandRunner(cmd_str).run()
            else:
                CommandRunner('gem install %s %s' % (opts, pkg_name)).run()

    def _handle_python_packages(self, packages):
        """very basic support for easy_install."""
        # TODO(asalkeld) support versions
        for pkg_name, versions in packages.iteritems():
            cmd_str = 'easy_install %s' % (pkg_name)
            CommandRunner(cmd_str).run()

    def _handle_yum_packages(self, packages):
        """Handle installation, upgrade, or downgrade of packages via yum.

        Arguments:
        packages -- a package entries map of the form:
                      "pkg_name" : "version",
                      "pkg_name" : ["v1", "v2"],
                      "pkg_name" : []

        For each package entry:
          * if no version is supplied and the package is already installed, do
            nothing
          * if no version is supplied and the package is _not_ already
            installed, install it
          * if a version string is supplied, and the package is already
            installed, determine whether to downgrade or upgrade (or do nothing
            if version matches installed package)
          * if a version array is supplied, choose the highest version from the
            array and follow same logic for version string above
        """
        # collect pkgs for batch processing at end
        installs = []
        downgrades = []
        for pkg_name, versions in packages.iteritems():
            ver = RpmHelper.newest_rpm_version(versions)
            pkg = "%s-%s" % (pkg_name, ver) if ver else pkg_name
            if RpmHelper.rpm_package_installed(pkg):
                # FIXME:print non-error, but skipping pkg
                pass
            elif not RpmHelper.yum_package_available(pkg):
                LOG.warn("Skipping package '%s'. Not available via yum" % pkg)
            elif not ver:
                installs.append(pkg)
            else:
                current_ver = RpmHelper.rpm_package_version(pkg)
                rc = RpmHelper.compare_rpm_versions(current_ver, ver)
                if rc < 0:
                    installs.append(pkg)
                elif rc > 0:
                    downgrades.append(pkg)
        if installs:
            RpmHelper.install(installs, rpms=False)
        if downgrades:
            RpmHelper.downgrade(downgrades)

    def _handle_rpm_packages(self, packages):
        """Handle installation, upgrade, or downgrade of packages via rpm.

        Arguments:
        packages -- a package entries map of the form:
                      "pkg_name" : "url"

        For each package entry:
          * if the EXACT package is already installed, skip it
          * if a different version of the package is installed, overwrite it
          * if the package isn't installed, install it
        """
        #FIXME: handle rpm installs
        pass

    def _handle_apt_packages(self, packages):
        """very basic support for apt."""
        # TODO(asalkeld) support versions
        pkg_list = ' '.join([p for p in packages])

        cmd_str = 'DEBIAN_FRONTEND=noninteractive apt-get -y install %s' % \
                  pkg_list
        CommandRunner(cmd_str).run()

    # map of function pointers to handle different package managers
    _package_handlers = {"yum": _handle_yum_packages,
                         "rpm": _handle_rpm_packages,
                         "apt": _handle_apt_packages,
                         "rubygems": _handle_gem_packages,
                         "python": _handle_python_packages}

    def _package_handler(self, manager_name):
        handler = None
        if manager_name in self._package_handlers:
            handler = self._package_handlers[manager_name]
        return handler

    def apply_packages(self):
        """Install, upgrade, or downgrade packages listed.

        Each package is a dict containing package name and a list of versions
        Install order:
          * dpkg
          * rpm
          * apt
          * yum
        """
        if not self._packages:
            return
        packages = sorted(self._packages.iteritems(), PackagesHandler._pkgsort)

        for manager, package_entries in packages:
            handler = self._package_handler(manager)
            if not handler:
                LOG.warn("Skipping invalid package type: %s" % manager)
            else:
                handler(self, package_entries)


class FilesHandler(object):
    def __init__(self, files):
        self._files = files

    def apply_files(self):
        if not self._files:
            return
        for fdest, meta in self._files.iteritems():
            dest = fdest.encode()
            try:
                os.makedirs(os.path.dirname(dest))
            except OSError as e:
                if e.errno == errno.EEXIST:
                    LOG.debug(str(e))
                else:
                    LOG.exception(e)

            if 'content' in meta:
                if isinstance(meta['content'], basestring):
                    f = open(dest, 'w+')
                    f.write(meta['content'])
                    f.close()
                else:
                    f = open(dest, 'w+')
                    f.write(json.dumps(meta['content'], indent=4))
                    f.close()
            elif 'source' in meta:
                CommandRunner('wget -O %s %s' % (dest, meta['source'])).run()
            else:
                LOG.error('%s %s' % (dest, str(meta)))
                continue

            uid = -1
            gid = -1
            if 'owner' in meta:
                try:
                    user_info = pwd.getpwnam(meta['owner'])
                    uid = user_info[2]
                except KeyError:
                    pass

            if 'group' in meta:
                try:
                    group_info = grp.getgrnam(meta['group'])
                    gid = group_info[2]
                except KeyError:
                    pass

            os.chown(dest, uid, gid)
            if 'mode' in meta:
                os.chmod(dest, int(meta['mode'], 8))


class SourcesHandler(object):
    '''tar, tar+gzip,tar+bz2 and zip.'''
    _sources = {}

    def __init__(self, sources):
        self._sources = sources

    def _url_to_tmp_filename(self, url):
        tempdir = tempfile.mkdtemp()
        atexit.register(lambda: shutil.rmtree(tempdir, True))
        name = os.path.basename(url)
        return os.path.join(tempdir, name)

    def _splitext(self, path):
        (r, ext) = os.path.splitext(path)
        return (r, ext.lower())

    def _github_ball_type(self, url):
        ext = ""
        if url.endswith('/'):
            url = url[0:-1]
        sp = url.split('/')
        if len(sp) > 2:
            http = sp[0].startswith('http')
            github = sp[2].endswith('github.com')
            btype = sp[-2]
            if http and github:
                if 'zipball' == btype:
                    ext = '.zip'
                elif 'tarball' == btype:
                    ext = '.tgz'
        return ext

    def _source_type(self, url):
        (r, ext) = self._splitext(url)
        if ext == '.gz':
            (r, ext2) = self._splitext(r)
            if ext2 == '.tar':
                ext = '.tgz'
        elif ext == '.bz2':
            (r, ext2) = self._splitext(r)
            if ext2 == '.tar':
                ext = '.tbz2'
        elif ext == "":
            ext = self._github_ball_type(url)

        return ext

    def _apply_source_cmd(self, dest, url):
        cmd = ""
        basename = os.path.basename(url)
        stype = self._source_type(url)
        if stype == '.tgz':
            cmd = "wget -q -O - '%s' | gunzip | tar -xvf -" % url
        elif stype == '.tbz2':
            cmd = "wget -q -O - '%s' | bunzip2 | tar -xvf -" % url
        elif stype == '.zip':
            tmp = self._url_to_tmp_filename(url)
            cmd = "wget -q -O '%s' '%s' && unzip -o '%s'" % (tmp, url, tmp)
        elif stype == '.tar':
            cmd = "wget -q -O - '%s' | tar -xvf -" % url
        elif stype == '.gz':
            (r, ext) = self._splitext(basename)
            cmd = "wget -q -O - '%s' | gunzip > '%s'" % (url, r)
        elif stype == '.bz2':
            (r, ext) = self._splitext(basename)
            cmd = "wget -q -O - '%s' | bunzip2 > '%s'" % (url, r)

        if cmd != '':
            cmd = "mkdir -p '%s'; cd '%s'; %s" % (dest, dest, cmd)

        return cmd

    def _apply_source(self, dest, url):
        cmd = self._apply_source_cmd(dest, url)
        if cmd != '':
            runner = CommandRunner(cmd)
            runner.run()

    def apply_sources(self):
        if not self._sources:
            return
        for dest, url in self._sources.iteritems():
            self._apply_source(dest, url)


class ServicesHandler(object):
    _services = {}

    def __init__(self, services, resource=None, hooks=None):
        self._services = services
        self.resource = resource
        self.hooks = hooks

    def _handle_sysv_command(self, service, command):
        service_exe = "/sbin/service"
        enable_exe = "/sbin/chkconfig"
        cmd = ""
        if "enable" == command:
            cmd = "%s %s on" % (enable_exe, service)
        elif "disable" == command:
            cmd = "%s %s off" % (enable_exe, service)
        elif "start" == command:
            cmd = "%s %s start" % (service_exe, service)
        elif "stop" == command:
            cmd = "%s %s stop" % (service_exe, service)
        elif "status" == command:
            cmd = "%s %s status" % (service_exe, service)
        command = CommandRunner(cmd)
        command.run()
        return command

    def _handle_systemd_command(self, service, command):
        exe = "/bin/systemctl"
        cmd = ""
        service = '%s.service' % service
        if "enable" == command:
            cmd = "%s enable %s" % (exe, service)
        elif "disable" == command:
            cmd = "%s disable %s" % (exe, service)
        elif "start" == command:
            cmd = "%s start %s" % (exe, service)
        elif "stop" == command:
            cmd = "%s stop %s" % (exe, service)
        elif "status" == command:
            cmd = "%s status %s" % (exe, service)
        command = CommandRunner(cmd)
        command.run()
        return command

    def _initialize_service(self, handler, service, properties):
        if "enabled" in properties:
            enable = to_boolean(properties["enabled"])
            if enable:
                LOG.info("Enabling service %s" % service)
                handler(self, service, "enable")
            else:
                LOG.info("Disabling service %s" % service)
                handler(self, service, "disable")

        if "ensureRunning" in properties:
            ensure_running = to_boolean(properties["ensureRunning"])
            command = handler(self, service, "status")
            running = command.status == 0
            if ensure_running and not running:
                LOG.info("Starting service %s" % service)
                handler(self, service, "start")
            elif not ensure_running and running:
                LOG.info("Stopping service %s" % service)
                handler(self, service, "stop")

    def _monitor_service(self, handler, service, properties):
        if "ensureRunning" in properties:
            ensure_running = to_boolean(properties["ensureRunning"])
            command = handler(self, service, "status")
            running = command.status == 0
            if ensure_running and not running:
                LOG.warn("Restarting service %s" % service)
                start_cmd = handler(self, service, "start")
                if start_cmd.status != 0:
                    LOG.warning('Service %s did not start. STDERR: %s' %
                               (service, start_cmd.stderr))
                    return
                for h in self.hooks:
                    h.event('service.restarted', service, self.resource)

    def _monitor_services(self, handler, services):
        for service, properties in services.iteritems():
            self._monitor_service(handler, service, properties)

    def _initialize_services(self, handler, services):
        for service, properties in services.iteritems():
            self._initialize_service(handler, service, properties)

    # map of function pointers to various service handlers
    _service_handlers = {
        "sysvinit": _handle_sysv_command,
        "systemd": _handle_systemd_command
    }

    def _service_handler(self, manager_name):
        handler = None
        if manager_name in self._service_handlers:
            handler = self._service_handlers[manager_name]
        return handler

    def apply_services(self):
        """Starts, stops, enables, disables services."""
        if not self._services:
            return
        for manager, service_entries in self._services.iteritems():
            handler = self._service_handler(manager)
            if not handler:
                LOG.warn("Skipping invalid service type: %s" % manager)
            else:
                self._initialize_services(handler, service_entries)

    def monitor_services(self):
        """Restarts failed services, and runs hooks."""
        if not self._services:
            return
        for manager, service_entries in self._services.iteritems():
            handler = self._service_handler(manager)
            if not handler:
                LOG.warn("Skipping invalid service type: %s" % manager)
            else:
                self._monitor_services(handler, service_entries)


class ConfigsetsHandler(object):

    def __init__(self, configsets, selectedsets):
        self.configsets = configsets
        self.selectedsets = selectedsets

    def expand_sets(self, list, executionlist):
        for elem in list:
            if isinstance(elem, dict):
                dictkeys = elem.keys()
                if len(dictkeys) != 1 or dictkeys.pop() != 'ConfigSet':
                    raise Exception('invalid ConfigSets metadata')
                dictkey = elem.values().pop()
                try:
                    self.expand_sets(self.configsets[dictkey], executionlist)
                except KeyError:
                    raise Exception("Undefined ConfigSet '%s' referenced"
                                    % dictkey)
            else:
                executionlist.append(elem)

    def get_configsets(self):
        """Returns a list of Configsets to execute in template."""
        if not self.configsets:
            if self.selectedsets:
                raise Exception('Template has no configSets')
            return
        if not self.selectedsets:
            if 'default' not in self.configsets:
                raise Exception('Template has no default configSet, must'
                                ' specify')
            self.selectedsets = 'default'

        selectedlist = [x.strip() for x in self.selectedsets.split(',')]
        executionlist = []
        for item in selectedlist:
            if item not in self.configsets:
                raise Exception("Requested configSet '%s' not in configSets"
                                " section" % item)
            self.expand_sets(self.configsets[item], executionlist)
        if not executionlist:
            raise Exception(
                "Requested configSet %s empty?" % self.selectedsets)

        return executionlist


def metadata_server_port(
        datafile='/var/lib/heat-cfntools/cfn-metadata-server'):
    """Return the the metadata server port.

    Reads the :NNNN from the end of the URL in cfn-metadata-server
    """
    try:
        f = open(datafile)
        server_url = f.read().strip()
        f.close()
    except IOError:
        return None

    if len(server_url) < 1:
        return None

    if server_url[-1] == '/':
        server_url = server_url[:-1]

    try:
        return int(server_url.split(':')[-1])
    except ValueError:
        return None


class CommandsHandlerRunError(Exception):
    pass


class CommandsHandler(object):

    def __init__(self, commands):
        self.commands = commands

    def apply_commands(self):
        """Execute commands on the instance in alphabetical order by name."""
        if not self.commands:
            return
        for command_label in sorted(self.commands):
            LOG.debug("%s is being processed" % command_label)
            self._initialize_command(command_label,
                                     self.commands[command_label])

    def _initialize_command(self, command_label, properties):
        command_status = None
        cwd = None
        env = properties.get("env", None)

        if "cwd" in properties:
            cwd = os.path.expanduser(properties["cwd"])
            if not os.path.exists(cwd):
                LOG.error("%s has failed. " % command_label +
                          "%s path does not exist" % cwd)
                return

        if "test" in properties:
            test = CommandRunner(properties["test"])
            test_status = test.run('root', cwd, env).status
            if test_status != 0:
                LOG.info("%s test returns false, skipping command"
                         % command_label)
                return
            else:
                LOG.debug("%s test returns true, proceeding" % command_label)

        if "command" in properties:
            try:
                command = properties["command"]
                if isinstance(command, list):
                    escape = lambda x: '"%s"' % x.replace('"', '\\"')
                    command = ' '.join(map(escape, command))
                command = CommandRunner(command)
                command.run('root', cwd, env)
                command_status = command.status
            except OSError as e:
                if e.errno == errno.EEXIST:
                    LOG.debug(str(e))
                else:
                    LOG.exception(e)
        else:
            LOG.error("%s has failed. " % command_label
                      + "'command' property missing")
            return

        if command_status == 0:
            LOG.info("%s has been successfully executed" % command_label)
        else:
            if "ignoreErrors" in properties and \
               to_boolean(properties["ignoreErrors"]):
                LOG.info("%s has failed (status=%d). Explicit ignoring"
                         % (command_label, command_status))
            else:
                raise CommandsHandlerRunError("%s has failed." % command_label)


class GroupsHandler(object):

    def __init__(self, groups):
        self.groups = groups

    def apply_groups(self):
        """Create Linux/UNIX groups and assign group IDs."""
        if not self.groups:
            return
        for group, properties in self.groups.iteritems():
            LOG.debug("%s group is being created" % group)
            self._initialize_group(group, properties)

    def _initialize_group(self, group, properties):
        gid = properties.get("gid", None)

        param_list = []
        param_list.append(group)

        if gid is not None:
            param_list.append("--gid " + gid)

        command = CommandRunner("groupadd " + ' '.join(param_list))
        command.run()
        command_status = command.status

        if command_status == 0:
            LOG.info("%s has been successfully created" % group)
        elif command_status == 9:
            LOG.error("An error occured creating %s group : " %
                      group + "group name not unique")
        elif command_status == 4:
            LOG.error("An error occured creating %s group : " %
                      group + "GID not unique")
        elif command_status == 3:
            LOG.error("An error occured creating %s group : " %
                      group + "GID not valid")
        elif command_status == 2:
            LOG.error("An error occured creating %s group : " %
                      group + "Invalid syntax")
        else:
            LOG.error("An error occured creating %s group" % group)


class UsersHandler(object):

    def __init__(self, users):
        self.users = users

    def apply_users(self):
        """Create Linux/UNIX users and assign user IDs, groups and homedir."""
        if not self.users:
            return
        for user, properties in self.users.iteritems():
            LOG.debug("%s user is being created" % user)
            self._initialize_user(user, properties)

    def _initialize_user(self, user, properties):
        uid = properties.get("uid", None)
        homeDir = properties.get("homeDir", None)

        param_list = []
        param_list.append(user)

        if uid is not None:
            param_list.append("--uid " + uid)

        if homeDir is not None:
            param_list.append("--home " + homeDir)

        if "groups" in properties:
            param_list.append("--groups " + ','.join(properties["groups"]))

        #Users are created as non-interactive system users with a shell
        #of /sbin/nologin. This is by design and cannot be modified.
        param_list.append("--shell /sbin/nologin")

        command = CommandRunner("useradd " + ' '.join(param_list))
        command.run()
        command_status = command.status

        if command_status == 0:
            LOG.info("%s has been successfully created" % user)
        elif command_status == 9:
            LOG.error("An error occured creating %s user : " %
                      user + "user name not unique")
        elif command_status == 6:
            LOG.error("An error occured creating %s user : " %
                      user + "group does not exist")
        elif command_status == 4:
            LOG.error("An error occured creating %s user : " %
                      user + "UID not unique")
        elif command_status == 3:
            LOG.error("An error occured creating %s user : " %
                      user + "Invalid argument")
        elif command_status == 2:
            LOG.error("An error occured creating %s user : " %
                      user + "Invalid syntax")
        else:
            LOG.error("An error occured creating %s user" % user)


class MetadataServerConnectionError(Exception):
    pass


class Metadata(object):
    _metadata = None
    _init_key = "AWS::CloudFormation::Init"
    DEFAULT_PORT = 8000

    def __init__(self, stack, resource, access_key=None,
                 secret_key=None, credentials_file=None, region=None,
                 configsets=None):

        self.stack = stack
        self.resource = resource
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.credentials_file = credentials_file
        self.access_key = access_key
        self.secret_key = secret_key
        self.configsets = configsets

        # TODO(asalkeld) is this metadata for the local resource?
        self._is_local_metadata = True
        self._metadata = None
        self._has_changed = False

    def remote_metadata(self):
        """Connect to the metadata server and retreive the metadata."""

        if self.credentials_file:
            credentials = parse_creds_file(self.credentials_file)
            access_key = credentials['AWSAccessKeyId']
            secret_key = credentials['AWSSecretKey']
        elif self.access_key and self.secret_key:
            access_key = self.access_key
            secret_key = self.secret_key
        else:
            raise MetadataServerConnectionError("No credentials!")

        port = metadata_server_port() or self.DEFAULT_PORT

        client = cloudformation.CloudFormationConnection(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            is_secure=False, port=port,
            path="/v1", debug=0)

        res = client.describe_stack_resource(self.stack, self.resource)
        # Note pending upstream patch will make this response a
        # boto.cloudformation.stack.StackResourceDetail object
        # which aligns better with all the existing calls
        # see https://github.com/boto/boto/pull/857
        resource_detail = res['DescribeStackResourceResponse'][
            'DescribeStackResourceResult']['StackResourceDetail']
        return resource_detail['Metadata']

    def get_nova_meta(self,
                      cache_path='/var/lib/heat-cfntools/nova_meta.json'):
        """Get nova's meta_data.json and cache it.

        Since this is called repeatedly return the cached metadata,
        if we have it.
        """

        url = 'http://169.254.169.254/openstack/2012-08-10/meta_data.json'
        if not os.path.exists(cache_path):
            CommandRunner('wget -O %s %s' % (cache_path, url)).run()
        try:
            with open(cache_path) as fd:
                try:
                    return json.load(fd)
                except ValueError:
                    pass
        except IOError:
            pass
        return None

    def get_instance_id(self):
        """Get the unique identifier for this server."""
        instance_id = None
        md = self.get_nova_meta()
        if md is not None:
            instance_id = md.get('uuid')
        return instance_id

    def get_tags(self):
        """Get the tags for this server."""
        tags = {}
        md = self.get_nova_meta()
        if md is not None:
            tags.update(md.get('meta', {}))
            tags['InstanceId'] = md['uuid']
        return tags

    def retrieve(
            self,
            meta_str=None,
            default_path='/var/lib/heat-cfntools/cfn-init-data',
            last_path='/var/cache/heat-cfntools/last_metadata'):
        """Read the metadata from the given filename or from the remote server.

           Returns:
               True -- success
              False -- error
        """
        if meta_str:
            self._data = meta_str
        else:
            try:
                self._data = self.remote_metadata()
            except MetadataServerConnectionError as ex:
                LOG.warn("Unable to retrieve remote metadata : %s" % str(ex))

                # If reading remote metadata fails, we fall-back on local files
                # in order to get the most up-to-date version, we try:
                # /var/cache/heat-cfntools/last_metadata, followed by
                # /var/lib/heat-cfntools/cfn-init-data
                # This should allow us to do the right thing both during the
                # first cfn-init run (when we only have cfn-init-data), and
                # in the event of a temporary interruption to connectivity
                # affecting cfn-hup, in which case we want to use the locally
                # cached metadata or the logic below could re-run a stale
                # cfn-init-data
                fd = None
                for filepath in [last_path, default_path]:
                    try:
                        fd = open(filepath)
                    except IOError:
                        LOG.warn("Unable to open local metadata : %s" %
                                 filepath)
                        continue
                    else:
                        LOG.info("Opened local metadata %s" % filepath)
                        break

                if fd:
                    self._data = fd.read()
                    fd.close()
                else:
                    LOG.error("Unable to read any valid metadata!")
                    return

        if isinstance(self._data, str):
            self._metadata = json.loads(self._data)
        else:
            self._metadata = self._data

        cm = hashlib.md5(json.dumps(self._metadata))
        current_md5 = cm.hexdigest()
        old_md5 = None

        try:
            with open(last_path) as lm:
                om = hashlib.md5()
                om.update(lm.read())
                old_md5 = om.hexdigest()
        except Exception:
            pass
        if old_md5 != current_md5:
            self._has_changed = True

        # if cache dir does not exist try to create it
        cache_dir = os.path.dirname(last_path)
        if not os.path.isdir(cache_dir):
            try:
                os.makedirs(cache_dir, mode=0o700)
            except IOError as e:
                LOG.warn('could not create metadata cache dir %s [%s]' %
                         (cache_dir, e))
                return
        # save current metadata to file
        tmp_dir = os.path.dirname(last_path)
        with tempfile.NamedTemporaryFile(dir=tmp_dir,
                                         mode='wb',
                                         delete=False) as cf:
            os.chmod(cf.name, 0o600)
            cf.write(json.dumps(self._metadata))
            os.rename(cf.name, last_path)

        return True

    def __str__(self):
        return json.dumps(self._metadata)

    def display(self, key=None):
        """Print the metadata to the standard output stream. By default the
        full metadata is displayed but the ouptut can be limited to a specific
        with the <key> argument.

        Arguments:
            key -- the metadata's key to display, nested keys can be specified
                   separating them by the dot character.
                        e.g., "foo.bar"
                   If the key contains a dot, it should be surrounded by single
                   quotes
                        e.g., "foo.'bar.1'"
        """
        if self._metadata is None:
            return

        if key is None:
            print(str(self))
            return

        value = None
        md = self._metadata
        while True:
            key_match = re.match(r'^(?:(?:\'([^\']+)\')|([^\.]+))(?:\.|$)',
                                 key)
            if not key_match:
                break

            k = key_match.group(1) or key_match.group(2)
            if isinstance(md, dict) and k in md:
                key = key.replace(key_match.group(), '')
                value = md = md[k]
            else:
                break

        if key != '':
            value = None

        if value is not None:
            print(json.dumps(value))

        return

    def _is_valid_metadata(self):
        """Should find the AWS::CloudFormation::Init json key."""
        is_valid = self._metadata and \
            self._init_key in self._metadata and \
            self._metadata[self._init_key]
        if is_valid:
            self._metadata = self._metadata[self._init_key]
        return is_valid

    def _process_config(self, config="config"):
        """Parse and process a config section.

          * packages
          * sources
          * groups
          * users
          * files
          * commands
          * services
        """

        try:
            self._config = self._metadata[config]
        except KeyError:
            raise Exception("Could not find '%s' set in template, may need to"
                            " specify another set." % config)
        PackagesHandler(self._config.get("packages")).apply_packages()
        SourcesHandler(self._config.get("sources")).apply_sources()
        GroupsHandler(self._config.get("groups")).apply_groups()
        UsersHandler(self._config.get("users")).apply_users()
        FilesHandler(self._config.get("files")).apply_files()
        CommandsHandler(self._config.get("commands")).apply_commands()
        ServicesHandler(self._config.get("services")).apply_services()

    def cfn_init(self):
        """Process the resource metadata."""
        if not self._is_valid_metadata():
            raise Exception("invalid metadata")
        else:
            executionlist = ConfigsetsHandler(self._metadata.get("configSets"),
                                              self.configsets).get_configsets()
            if not executionlist:
                self._process_config()
            else:
                for item in executionlist:
                    self._process_config(item)

    def cfn_hup(self, hooks):
        """Process the resource metadata."""
        if not self._is_valid_metadata():
            LOG.debug(
                'Metadata does not contain a %s section' % self._init_key)

        if self._is_local_metadata:
            self._config = self._metadata.get("config", {})
            s = self._config.get("services")
            sh = ServicesHandler(s, resource=self.resource, hooks=hooks)
            sh.monitor_services()

        if self._has_changed:
            for h in hooks:
                h.event('post.update', self.resource, self.resource)
