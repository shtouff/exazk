#!/usr/bin/env python

import yaml
import time
import sys
import signal
import os
import string
import logging
import logging.handlers
import subprocess
import argparse

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import SessionExpiredError
from kazoo.handlers.threading import KazooTimeoutError

# ip_address
try:
    # Python 3.3+ or backport
    from ipaddress import ip_address as _ip_address  # pylint: disable=F0401

    def ip_address(x):
        try:
            x = x.decode('ascii')
        except AttributeError:
            pass
        return _ip_address(x)
except ImportError:
    # Python 2.6, 2.7, 3.2
    from ipaddr import IPAddress as ip_address
try:
    # Python 3.4+
    from enum import Enum
except ImportError:
    # Other versions. This is not really an enum but this is OK for
    # what we want to do.
    def Enum (*sequential):
        return type(str("Enum"), (), dict(zip(sequential, sequential)))

logger = logging.getLogger()
kzlogger = logging.getLogger('kazoo.client')

def parse ():
    """Parse arguments"""
    parser = argparse.ArgumentParser(description=sys.modules[__name__].__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--config", "-f", metavar="FILE", type=open, dest="conffile",
                        help="read configuration from file FILE. Will cancel any cmdline option")

    g = parser.add_argument_group("logging options")
    g.add_argument("--debug", "-d", action="store_true",
                   default=False,
                   help="enable debugging, disable syslog logging")
    g.add_argument("--silent", "-s", action="store_true",
                   default=False,
                   help="don't log to console")
    g.add_argument("--no-syslog", action="store_false", dest='syslog',
                   help="disable syslog logging")
    g.add_argument("--syslog-facility", "-sF", metavar="FACILITY",
                   nargs='?',
                   const="daemon",
                   default="daemon",
                   help="log to syslog using FACILITY, default FACILITY is daemon")

    g = parser.add_argument_group("ZooKeeper options")
    g.add_argument("--zk-host", "-zH", metavar='HOST',
                   type=str, dest="zk_hosts", action="append",
                   help="one of the ZooKeeper HOST to connect to")
    g.add_argument("--zk-path-service", "-zPS", dest="zk_path_service", metavar='ZKKEY',
                   type=str,
                   help="the ZKKEY path in ZooKeeper, where this instance should write if it's alive")
    g.add_argument("--zk-path-maintenance", "-zPM", dest="zk_path_maintenance", metavar='ZKKEY',
                   type=str,
                   help="if ZKKEY exists in ZooKeeper, the service is considered disabled")

    g = parser.add_argument_group("local check options")
    g.add_argument("--local-check", "-c", metavar='CMD',
                   type=str,
                   help="command to use for local check of service")

    g = parser.add_argument_group("advertising options")
    g.add_argument("--name", "-n", dest="srv_name", metavar='NAME',
                   type=str,
                   help="the service NAME of this instance")
    g.add_argument("--auth-ip", "-A", dest="srv_auth_ip", metavar='IP',
                   type=ip_address,
                   help="the IP this instance is authoritative for")
    g.add_argument("--non-auth-ip", "-N", metavar='IP',
                   type=ip_address, dest="srv_non_auth_ips", action="append",
                   help="one of the IP addresses this instance is non authoritative for")

    options = parser.parse_args()
    return options

def setup_logging (debug, silent, name, syslog_facility, syslog):
    """Setup logger"""

    logger.setLevel(debug and logging.DEBUG or logging.INFO)
    kzlogger.setLevel(debug and logging.DEBUG or logging.INFO)

    # syslog
    def syslog_address():
        """Return a sensitive syslog address"""
        if sys.platform == "darwin":
            return "/var/run/syslog"
        if sys.platform.startswith("freebsd"):
            return "/var/run/log"
        if sys.platform.startswith("linux"):
            return "/dev/log"
        raise EnvironmentError("Unable to guess syslog address for your "
                "platform, try to disable syslog")

    if syslog and not debug:
        facility = getattr(logging.handlers.SysLogHandler,
                "LOG_{0}".format(string.upper(syslog_facility)))
        sh = logging.handlers.SysLogHandler(address=str(syslog_address()),
                facility=facility)
        sh.setFormatter(logging.Formatter(
            "exazk-{0}[{1}]: %(name)s: %(message)s".format(
                name, os.getpid())))
        logger.addHandler(sh)

    # stderr
    if sys.stderr.isatty() and not silent:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(levelno)s: %(name)s: %(message)s"))
        logger.addHandler(ch)

    # no log at all
    if silent and not syslog:
        nh = logging.NullHandler()
        logger.addHandler(nh)

class Alarm(Exception):
    pass
def alarm_signal_handler (number, frame):  # pylint: disable=W0613
    raise Alarm()

class MaintenanceChecker:
    def __init__(self, zk, zk_path):
        if not isinstance(zk, KazooClient):
            raise Exception('KazooClient object expected')

        self.zk = zk
        self.zk_path = zk_path

    """ return True if maintenance mode engaged
    """
    def check(self):
        try:
            if self.zk.exists(self.zk_path):
                logger.warn('maintenance mode engaged ...')
                return True
        except SessionExpiredError as e:
            return False

class ServiceChecker:
    def __init__(self, command):
        self.command = command

    def check(self):
        try:
            signal.signal(signal.SIGALRM, alarm_signal_handler)
            signal.alarm(1)

            DEVNULL = open(os.devnull, 'w')
            p = subprocess.Popen(self.command, shell=True,
                    stdout=DEVNULL, stderr=DEVNULL, close_fds=True,
                    preexec_fn=os.setpgrp)

            rc = p.wait()
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
            if rc == 0:
                return True
            else:
                logger.error('local check returned: %s' % rc)

        except Alarm:
            logger.error("local check spent more than 1s to run")
            os.killpg(p.pid, signal.SIGKILL)

        return False

class BGPTable:
    def __init__(self):
        self.announce = []
        self.withdraw= []

    def add_route(self, **route):
        if 'prefix' not in route or 'metric' not in route:
            raise Exception('prefix & metric are mandatory in route')
        logger.debug('adding BGP route: %s' % route['prefix'])
        self.announce.append(route)

    def del_route(self, **route):
        if 'prefix' not in route:
            raise Exception('prefix is mandatory in route')
        logger.debug('deleting BGP route: %s' % route['prefix'])
        self.withdraw.append(route)

    def get_routes(self):
        return (self.announce, self.withdraw)

class BGPSpeaker:

    def __init__(self, table):
        if not isinstance(table, BGPTable):
            raise Exception('BGPTable object expected')

        self.table = table

    def advertise_routes(self):
        logger.info("advertising routes")
        (announce, withdraw) = self.table.get_routes()

        for route in announce:
            print('announce route %s/32 next-hop self med %s' %
                (route['prefix'], route['metric']))
        sys.stdout.flush()

        for route in withdraw:
            print('withdraw route %s/32' % route['prefix'])
        sys.stdout.flush()


class EZKConfFactory:
    def create_from_yaml_file(self, path):
        logger.debug('creating from YAML')
        yml = yaml.safe_load(path)

        return EZKConf(**yml)

    def create_from_options(self, options):

        if not isinstance(options, argparse.Namespace):
            raise Exception('Namespace object expected')

        return EZKConf(**options.__dict__)

class EZKConf:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __str__(self):
        return str(self.__dict__)

class EZKRuntime:
    def __init__(self, conf, zk):
        if not isinstance(conf, EZKConf):
            raise Exception('EZKConf object expected')

        if not isinstance(zk, KazooClient):
            raise Exception('KazooClient expected')

        self.conf = conf
        self.zk = zk
        self.bgp_table = BGPTable()

        # flags
        self.refresh = True
        self.recreate = True
        self.shouldstop = False

        # slow & fast cycles
        self.longsleep = 10
        self.shortsleep = 0.1

    def set_bgp_table(self, table):
        if not isinstance(table, BGPTable):
            raise Exception('BGPTable object expected')

        self.bgp_table = table

    def get_bgp_table(self):
        return self.bgp_table

    def get_conf(self):
        return self.conf

    def get_zk(self):
        return self.zk

    def create_node(self):
        self.recreate = False
        logger.info('re-creating my ephemeral node')

        try:
            self.get_zk().create('%s/%s' % (
                self.get_conf().zk_path_service,
                self.get_conf().srv_auth_ip), ephemeral=True)
        except SessionExpiredError as e:
            pass

    def refresh_children(self):
        self.refresh = False
        logger.info('refreshing children & routes')

        try:
            children = self.get_zk().get_children(self.get_conf().zk_path_service)
            bgp_table = BGPTable()

            for ip in self.get_conf().srv_non_auth_ips:
                if ip not in children:
                    bgp_table.add_route(prefix=ip, metric=200)
                else:
                    bgp_table.del_route(prefix=ip)

            bgp_table.add_route(prefix=runtime.get_conf().srv_auth_ip, metric=100)
            self.set_bgp_table(bgp_table)
        except SessionExpiredError as e:
            pass

    def withdraw_all(self):
        logging.info('withdrawing all routes')
        bgp_table = BGPTable()
        for ip in self.get_conf().srv_non_auth_ips:
            bgp_table.del_route(prefix=ip)
        bgp_table.del_route(prefix=self.get_conf().srv_auth_ip)
        self.set_bgp_table(bgp_table)

    def trigger_refresh(self):
        self.refresh = True

    def trigger_recreate(self):
        self.recreate = True

def main():
    global runtime

    options = parse()

    if options.conffile:
        conf = EZKConfFactory().create_from_yaml_file(options.conffile)
    else:
        conf = EZKConfFactory().create_from_options(options)

    setup_logging(conf.debug, conf.silent, conf.srv_name,
                  conf.syslog_facility, conf.syslog)

    logger.warn('ExaZK starting...')
    logger.debug('debug is active')
    zk = KazooClient(hosts=','.join(conf.zk_hosts))
    runtime = EZKRuntime(conf=conf, zk=zk)

    # exits gracefully when possible
    def exit_signal_handler(signal, frame):
        logger.warn('received signal %s, preparing to stop' % signal)
        runtime.shouldstop = True

    signal.signal(signal.SIGINT, exit_signal_handler)
    signal.signal(signal.SIGTERM, exit_signal_handler)

    def zk_transition(state):
        logger.info('zk state changed to %s' % state)

        if state == KazooState.SUSPENDED:
            logger.error('zk disconnected, flushing routes...')
            runtime.withdraw_all()

        if state == KazooState.LOST:
            logger.error('zk lost, have to re-create ephemeral node')
            runtime.trigger_recreate()

        if state == KazooState.CONNECTED:
            runtime.trigger_refresh()

    try:
        runtime.get_zk().start()
    except KazooTimeoutError as e:
        logger.error("can't connect to zk, aborting...")
        exit(1)

    runtime.get_zk().add_listener(zk_transition)
    runtime.get_zk().ensure_path(runtime.get_conf().zk_path_service)

    while runtime.get_zk().exists('%s/%s' %
            (runtime.get_conf().zk_path_service, runtime.get_conf().srv_auth_ip)):

        logger.warn('stale node found, sleeping(1)...')
        time.sleep(1)

    @zk.ChildrenWatch(runtime.get_conf().zk_path_service)
    def zk_watch(children):
        logger.debug('zk children are %s' % children)
        runtime.trigger_refresh()

    while not runtime.shouldstop:

        now = start = time.time()
        while not runtime.refresh and not runtime.recreate \
                and now<start+runtime.longsleep \
                and not runtime.shouldstop:

            time.sleep(runtime.shortsleep)
            now = time.time()

        if runtime.shouldstop:
            break

        if runtime.recreate:
            runtime.create_node()

        if not ServiceChecker(runtime.get_conf().local_check).check() \
                or MaintenanceChecker(runtime.get_zk(), runtime.get_conf().zk_path_maintenance).check():
            runtime.withdraw_all()
        elif runtime.get_zk().state == KazooState.CONNECTED:
            runtime.refresh_children()

        BGPSpeaker(runtime.get_bgp_table()).advertise_routes()

    # main loop exited, cleaning resources
    try:
        runtime.get_zk().stop()
        runtime.get_zk().close()
        logger.info('ExaZK stopped')
    except Exception as e:
        logger.error('did my best but something went wrong while stopping :(')

if __name__ == '__main__':
    main()
