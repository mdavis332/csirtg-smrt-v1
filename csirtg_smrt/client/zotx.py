from csirtg_indicator import Indicator, InvalidIndicator
from csirtg_smrt.constants import VERSION
from datetime import datetime, timezone, timedelta
from OTXv2 import OTXv2
import logging
import re

logger = logging.getLogger(__name__)

# AlienVault/LevelBlue OTX api client
class _OTX(object):
    __name__ = 'otx'

    def __init__(self, **kwargs):
        api_key = kwargs.get('token')
        client_metatag = 'csirtg-smrt {}'.format(VERSION)
        remote = kwargs.get('remote', 'https://otx.alienvault.com')

        self.client = OTXv2(api_key, server=remote, user_agent=client_metatag)

    def _get_group_indicators(self, group_id: str, lookback: dict, limit=1000):
        """
        For a given group_id, get list of indicators (IOCs)
        :param group_id: Group ID from which to get indicators
        :return: Indicator list
        """

        if isinstance(group_id, int):
            group_id = str(group_id)
        if not isinstance(group_id, str) or not re.match(r'^[0-9]{1,6}$', group_id):
            raise Exception('group_id should be an integer between 1-6 digits')
        
        endtime = datetime.now(timezone.utc)
        starttime = endtime - timedelta(**lookback)
        starttime = starttime.strftime('%Y-%m-%dT%H:%M:00')
        
        url = "{}/otxapi/groups/{}/pulses/?groupId={}&sort=-created&limit={}&start={}&include_inactive=0".format(
            self.client.server, group_id, group_id, limit, starttime)
        
        url = self.client.create_url(url)
        return self.client.walkapi(url)

    def ping(self, write=False):
        raise NotImplemented

    def indicators_create(self, data, **kwargs):
        raise NotImplemented

    def indicators(self, group_id=None, lookback={'hours': 1}):
        if not group_id:
            raise Exception('group_id must be specified for OTX fetcher')

        reports = self._get_group_indicators(group_id, lookback)

        for report in reports:
            indicator = {}
            indicator['additional_data'] = report['description']
            indicator['tlp'] = report['TLP']
            indicator['description'], indicator['reporttime'] = report['name'], report['created']
            indicator['firsttime'], indicator['lasttime'] = report['created'], report['modified']
            indicator['reference'] = 'https://otx.alienvault.com/pulse/{}'.format(report['id'])
            indicator['tags'] = set()

            for tag in report['tags']:
                indicator['tags'].add(tag.replace(' ', '_').lower())
            indicator['tags'] = list(indicator['tags'])

            for report_indicator in self.client.get_pulse_indicators(report['id']):
                indicator['indicator'] = report_indicator['indicator']
                if report_indicator['created']:
                    indicator['created'] = report_indicator['created']
                try:
                    cif_i = Indicator(**indicator)
                    yield cif_i
                except InvalidIndicator:
                    continue
