#! /usr/bin/env python

import socket
import sys
import yaml
import subprocess
import time
import logging
from kazoo.client import KazooClient, KazooState

logging.basicConfig()
logger = logging.getLogger()
logger.setLevel('DEBUG')

class ExaBGPInstance:
    def __init__(self):
        pass

    def announce(self, route, dest, metric):
        pass

class ExaZKService:

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
            logger.debug('ExaZKService: %s <= %s' % ( key, value ))

    def display(self):
        print(self.name)

class ExaZKConf:

    def __init__(self, **kwargs):
        yml = yaml.safe_load(open(kwargs['path']))

        self.zk_hosts = yml['zk_hosts']
        self.zk_path_maintenance = yml['zk_path_maintenance']
        self.zk_path_service = yml['zk_path_service']
        self.local_check = yml['local_check']

        self.services = {}
        for srv in yml['services']:
            self.services[srv] = ExaZKService( name = srv, **yml['services'][srv] )

    def getService(self, name):
        for key, value in self.services.items():
            if key == name:
                return value

        return None


class ExaZKBGPTable:

    def __init__(self):
        self.table = []

    def add_route(self, **kwargs):
        logger.debug('adding BGP route %s' % kwargs['prefix'])
        self.table.append(kwargs)

    def del_route(self, **kwargs):
        self.table.remove(kwargs)

    def get_routes(self):
        return self.table

class ExaZKRuntime:

    def __init__(self, conf):
        if not isinstance(conf, ExaZKConf):
            raise Exception('ExaZKConf object expected')

        self.bgp_table = ExaZKBGPTable()
        self.refresh = False
        self.recreate = False

    def set_bgp_table(self, bgp_table):
        self.bgp_table = bgp_table

    def get_bgp_table(self):
        return self.bgp_table

class ServiceCheckerThread():
    def __init__(self, conf):
        if not isinstance(conf, ExaZKConf):
            raise Exception('ExaZKConf object expected')

        self.command = conf.local_check

    def mustrun(self):
        return True

    def run(self):
        while self.mustrun():
            self.check()
            time.sleep(1)

    def check(self):
        if subprocess.Popen(self.command).wait() == 0:
            logger.debug('local check successed')
            return True
        else:
            logger.debug('local check failed')
            return False

class ExaZKState(KazooState):
    INIT = "INIT"


conf = ExaZKConf(path=sys.argv[1])
runtime = ExaZKRuntime(conf)

apex = conf.getService('apex')

ppath = '%s/apex' % conf.zk_path_service
path = '%s/%s' % (ppath, apex.auth_ip)
pstate = ExaZKState.INIT

def zk_listener(state):
    global pstate
    global runtime
    global apex
    global path
    global ppath
    global zk

    logger.debug('zk state changed to  %s' % state)
    if state == ExaZKState.SUSPENDED:
        logger.error('lost connection to zk, cleaning bgp routes')
        bgp_routes = ExaZKBGPTable()
        runtime.set_bgp_table(bgp_routes)

    if state == ExaZKState.LOST:
        logger.error('lost ephemeral nodes, re-creating them')
        #zk.create(path, socket.gethostname(), ephemeral=True)
        runtime.recreate = True
        runtime.refresh = True

    if state == ExaZKState.CONNECTED and pstate != ExaZKState.INIT:
        logger.error('we reconnected')
        runtime.refresh = True
        #zk

    pstate = state

zk = KazooClient(hosts=','.join(conf.zk_hosts))
zk.add_listener(zk_listener)
zk.start()

zk.ensure_path(ppath)

if zk.exists(path):
    logger.warn("deleting stale node %s" % path)
    zk.delete(path)

zk.create(path,
    socket.gethostname(), ephemeral=True)

@zk.ChildrenWatch(ppath)
def service_changed(children):
    global runtime
    print('Children are now: %s' % children)

    bgp_routes = ExaZKBGPTable()

    for ip in apex.non_auth_ips:
        if ip not in children:
            bgp_routes.add_route(prefix=ip, dst='1.1.1.1', metric=200)

    bgp_routes.add_route(prefix=apex.auth_ip, dst='1.1.1.1', metric=100)

    runtime.set_bgp_table(bgp_routes)

while True:
    time.sleep(1)

    if runtime.refresh:
        runtime.refresh = False
        bgp_routes = ExaZKBGPTable()
        children = zk.get_children(ppath)

        for ip in apex.non_auth_ips:
            if ip not in children:
                bgp_routes.add_route(prefix=ip, dst='1.1.1.1', metric=200)

        bgp_routes.add_route(prefix=apex.auth_ip, dst='1.1.1.1', metric=100)

        runtime.set_bgp_table(bgp_routes)

    if runtime.recreate:
        runtime.recreate = False

        zk.create(path,
            socket.gethostname(), ephemeral=True)

    for route in runtime.get_bgp_table().get_routes():
        print ('announce %s => %s metric %s' %
                (route['prefix'], route['dst'], route['metric']) )
    print ()



