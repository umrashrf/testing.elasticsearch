from testing.elasticsearch import Elasticsearch as ES


class Elasticsearch(ES):

    def initialize(self):
        super(Elasticsearch, self).initialize()
        self.DEFAULT_SETTINGS['boot_timeout'] = 60
        self.elasticsearch_yaml.pop('discovery.zen.ping.multicast.enabled')
        self.elasticsearch_yaml['discovery.zen.ping.unicast.hosts'] = ''
        self.elasticsearch_yaml['discovery.zen.minimum_master_nodes'] = 0
