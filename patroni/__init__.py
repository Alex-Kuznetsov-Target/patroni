import logging
import signal
import time

from patroni.api import RestApiServer
from patroni.config import Config
from patroni.dcs import get_dcs
from patroni.exceptions import DCSError
from patroni.ha import Ha
from patroni.postgresql import Postgresql
from patroni.utils import reap_children, set_ignore_sigterm, setup_signal_handlers
from patroni.version import __version__

logger = logging.getLogger(__name__)


class Patroni(object):

    def __init__(self):
        self.version = __version__
        self.config = Config()
        self.dcs = get_dcs(self.config)
        self.load_dynamic_configuration()

        self.postgresql = Postgresql(self.config['postgresql'])
        self.api = RestApiServer(self, self.config['restapi'])
        self.ha = Ha(self)

        self.tags = self.get_tags()
        self.nap_time = self.config['loop_wait']
        self.next_run = time.time()
        self.scheduled_restart = {}

        self._reload_config_scheduled = False
        self._received_sighup = False

    def load_dynamic_configuration(self):
        while True:
            try:
                cluster = self.dcs.get_cluster()
                if cluster and cluster.config:
                    self.config.set_dynamic_configuration(cluster.config)
                elif not self.config.dynamic_configuration and 'bootstrap' in self.config:
                    self.config.set_dynamic_configuration(self.config['bootstrap']['dcs'])
                break
            except DCSError:
                logger.warning('Can not get cluster from dcs')

    def get_tags(self):
        return {tag: value for tag, value in self.config.get('tags', {}).items()
                if tag not in ('clonefrom', 'nofailover', 'noloadbalance') or value}

    def reload_config(self):
        try:
            self.tags = self.get_tags()
            self.nap_time = self.config['loop_wait']
            self.dcs.set_ttl(self.config.get('ttl') or 30)
            self.dcs.set_retry_timeout(self.config.get('retry_timeout') or self.nap_time)
            self.api.reload_config(self.config['restapi'])
            self.postgresql.reload_config(self.config['postgresql'])
        except Exception:
            logger.exception('Failed to reload config_file=%s', self.config.config_file)

    def sighup_handler(self, *args):
        self._received_sighup = True

    @property
    def noloadbalance(self):
        return self.tags.get('noloadbalance', False)

    @property
    def nofailover(self):
        return self.tags.get('nofailover', False)

    @property
    def replicatefrom(self):
        return self.tags.get('replicatefrom')

    def schedule_next_run(self):
        self.next_run += self.nap_time
        current_time = time.time()
        nap_time = self.next_run - current_time
        if nap_time <= 0:
            self.next_run = current_time
        elif self.dcs.watch(nap_time):
            self.next_run = time.time()

    def run(self):
        self.api.start()
        signal.signal(signal.SIGHUP, self.sighup_handler)
        self.next_run = time.time()

        while True:
            if self._received_sighup:
                self._received_sighup = False
                if self.config.reload_local_configuration():
                    self.reload_config()

            logger.info(self.ha.run_cycle())

            cluster = self.dcs.cluster
            if cluster and cluster.config and self.config.set_dynamic_configuration(cluster.config):
                self.reload_config()

            if not self.postgresql.data_directory_empty():
                self.config.save_cache()

            reap_children()
            self.schedule_next_run()


def main():
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    logging.getLogger('requests').setLevel(logging.WARNING)
    setup_signal_handlers()

    patroni = Patroni()
    try:
        patroni.run()
    except KeyboardInterrupt:
        set_ignore_sigterm()
    finally:
        patroni.api.shutdown()
        patroni.postgresql.stop(checkpoint=False)
        patroni.dcs.delete_leader()
