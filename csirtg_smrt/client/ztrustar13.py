from csirtg_indicator import Indicator, InvalidIndicator
from csirtg_smrt.constants import VERSION
from datetime import datetime, timedelta
from trustar import TruStar, datetime_to_millis
import logging
import uuid

logger = logging.getLogger(__name__)

# TruStar v1.3 api client
class _TRUSTAR(object):
    __name__ = 'trustar13'

    def __init__(self, **kwargs):
        api_key = kwargs.get('token')
        api_secret = kwargs.get('token_secret')
        client_metatag = 'csirtg-smrt {}'.format(VERSION)
        remote = kwargs.get('remote', 'https://station.trustar.co')
        
        self.client = TruStar(config={'user_api_key': api_key, 
            'user_api_secret': api_secret,
            'client_metatag': client_metatag,
            'station_base_url': remote
        })

    def ping(self, write=False):
        return self.client.ping()

    def indicators_create(self, data, **kwargs):
        raise NotImplemented

    def indicators(self, enclave_ids=[], lookback={'hours': 1}):
        endtime = datetime.now()
        starttime = endtime - timedelta(**lookback)
        starttime = datetime_to_millis(starttime)
        endtime = datetime_to_millis(endtime)

        query_params = {
            'from_time': starttime,
            'to_time': endtime,
        }
        if enclave_ids:
            query_params['is_enclave'] = True
            query_params['enclave_ids'] = enclave_ids

        reports = self.client.get_reports(**query_params)

        for report in reports:
            indicator = {}
            indicator['additional_data'] = report.body
            indicator['description'], indicator['reporttime'] = report.title, report.time_began
            indicator['firsttime'], indicator['lasttime'] = report.created, report.updated
            indicator['uuid'] = report.id
            indicator['reference'] = 'https://station.trustar.co/constellation/reports/{}'.format(report.id)
            indicator['tags'] = set()

            for tag in self.client.get_enclave_tags(report.id):
                indicator['tags'].add(tag.name)
            indicator['tags'] = list(indicator['tags'])

            for report_indicator in self.client.get_indicators_for_report(report.id):
                indicator['indicator'] = report_indicator.value
                try:
                    cif_i = Indicator(**indicator)
                    yield cif_i
                except InvalidIndicator:
                    continue
