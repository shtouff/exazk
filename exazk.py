#!/usr/bin/env python

import yaml
import time
import sys
import signal
import os
import logging
import subprocess

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import SessionExpiredError
from kazoo.handlers.threading import KazooTimeoutError

logging.basicConfig()
logger = logging.getLogger()
logger.setLevel('DEBUG')

class Alarm(Exception):
    pass
def alarm_signal_handler (number, frame):  # pylint: disable=W0613
    raise Alarm()

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
        if 'prefix' not in route or 'dst' not in route or 'metric' not in route:
            raise Exception('prefix, dst & metric are mandatory in route')
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
        yml = yaml.safe_load(open(path))

        return EZKConf(**yml)

class EZKConf:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

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
                    bgp_table.add_route(prefix=ip, dst='1.1.1.1', metric=200)
                else:
                    bgp_table.del_route(prefix=ip)

            bgp_table.add_route(prefix=runtime.get_conf().srv_auth_ip,
                    dst='1.1.1.1', metric=100)
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

logger.info('ExaZK starting...')
conf = EZKConfFactory().create_from_yaml_file(sys.argv[1])
zk = KazooClient(hosts=','.join(conf.zk_hosts))
runtime = EZKRuntime(conf=conf, zk=zk)

# exits gracefully when possible
def exit_signal_handler(signal, frame):
    logger.info('received signal %s, preparing to stop' % signal)
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
            or runtime.get_zk().exists(runtime.get_conf().zk_path_maintenance):
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
