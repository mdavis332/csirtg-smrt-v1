'''
MISP client used to send fetched/parsed indicators _to_ a MISP instance
cli launch should look like the following:
> csirtg-smrt --skip-invalid --client misp --rule ~/push_to_misp.yml --remote http://<misp> \
  --token <MISP_write_token>

Standard smrt rule fields like "tags" can be placed at the rule-level or feed-level like normal. Rule-level
  fields will be applied to all attributes for all feeds. Feed-level key/values will be applied on a
  per-feed basis. e.g., in the below rule, the "ips" feed will inherit the rule-wide TLP:CLEAR and "testing"
  tags, while the "urls" feed attributes will be marked TLP:AMBER and be tagged as "phishing"
  defaults:
    tags:
      - testing
    tlp: clear
  feeds:
    ips:
      values:
        - indicator
    urls:
      values:
        - indicator
      defaults:
        tags:
          - phishing
        tlp: amber

Smrt rules for the MISP client can have a "to_misp" key under the rule-level or feed-level "defaults"
  sections. All key/values under the "to_misp" key will be used to set the same named elements
  for the event or attribute on the MISP side. e.g., 
  defaults:
    to_misp:
      attribute:
        to_ids: True
      event:
        id: 1234
        published: 1

If no destination event id field is specified in the rule, then a new event will be created in MISP
  for every fireball batch (default every 100 indicators)

If 
'''

import logging

from pymisp import PyMISP, MISPEvent, MISPAttribute, MISPOrganisation
from csirtg_indicator.exceptions import InvalidIndicator
from csirtg_smrt.client.plugin import Client

logger = logging.getLogger(__name__)

CIF_TO_MISP_TYPES = {
    'ipv4': 'ip-src',
    'ipv6': 'ip-src',
    'asn': 'AS',
    'fqdn': 'domain',
    'email': 'email',
    'md5': 'md5',
    'sha1': 'sha1',
    'sha256': 'sha256',
    'sha512': 'sha512',
    'ssdeep': 'ssdeep',
    'url': 'url',
}

SMRT_RULE_MISP_TOPLEVEL = 'to_misp'

class MISP(Client):

    def __init__(self, remote, token, **kwargs):

        super().__init__(remote, token)
        self.verify_ssl = kwargs.get('verify_ssl', True)
        self.client = PyMISP(remote, token, self.verify_ssl, 'json')
        
    def start(self):
        return True

    def stop(self):
        return True

    def _load_misp_settings_from_smrt_rule(self, **kwargs):
        self.rule = dict(kwargs['rule'].items())
        self.event = {}
        self.attribute = {}
        self.feed = kwargs.get('feed', '')
        rule_event = self.rule.get('defaults', {}).get(SMRT_RULE_MISP_TOPLEVEL, {}).get('event', {})
        feed_event = self.rule.get(self.feed, {}).get('defaults', {}).get(SMRT_RULE_MISP_TOPLEVEL, {}).get('event', {})
        rule_attr = self.rule.get('defaults', {}).get(SMRT_RULE_MISP_TOPLEVEL, {}).get('attribute', {})
        feed_attr = self.rule.get(self.feed, {}).get('defaults', {}).get(SMRT_RULE_MISP_TOPLEVEL, {}).get('attribute', {})
        # merge 2 dicts giving priority to kv's in last merged (feed-level)
        self.event = rule_event | feed_event
        self.attribute = rule_attr | feed_attr

        # if whole feed is same misp attrib type, allow manual input and don't resolve each
        self.misp_attrib_type = self.rule.get(self.feed, {}).get('itype') or \
           self.rule.get(self.feed, {}).get('defaults', {}).get('itype') or self.rule.get('itype')

    def _get_misp_event(self):
        '''fetches the MISP event to which attributes should be added. if one is not specified or
        doesn't exist, a new event is created.
        input: None
        output: MISPEvent() object
        '''
        dst_event_id = self.event.get('id', -1)
        event = self.client.get_event(dst_event_id, pythonify=True)
        self.new_event = False

        # if event doesn't exist, create new one
        if event.get('errors'):
            event = MISPEvent()
            self.new_event = True

        for setting, value in self.event.items():
            if setting == 'id':
                continue
            if setting == 'tags':
                tags = value
                if not isinstance(tags, list):
                    tags = [tags]
                for tag in tags:
                    event.add_tag(tag)
                continue
            setattr(event, setting, value)

        if self.new_event:
            # MISP api contract requires the "info" field
            if not getattr(event, 'info', None):
                event.info = self.rule.get(self.feed, {}).get('defaults', {}).get('provider') or \
                    self.rule.get('defaults', {}).get('provider')

            # reject remote sync edits
            # https://github.com/MISP/PyMISP/issues/239
            event.locked = True
            
            reference = self.rule.get(self.feed, {}).get('defaults', {}).get('reference') or \
                self.rule.get('defaults', {}).get('reference')
            if reference:
                event.add_attribute(**{'type': 'link', 'value': reference})
                event.add_tag('source:{}'.format(reference))

            # in MISP, it's customary to designate TLP at Event level rather than Attrib level
            tlp = self.rule.get('defaults', {}).get('tlp', 'amber')
            event.add_tag('tlp:{}'.format(tlp))

            if getattr(event, 'orgc_id', None) and event.orgc_id == 'provider':
                event.orgc_id = None
                # Set orgc from provider field
                # does orgc already exist?
                instance_orgs = self.client.organisations()
                for org in instance_orgs:
                    if org['Organisation']['name'] == self.rule['defaults']['provider']:
                        orgc = MISPOrganisation()
                        orgc.id = org['Organisation']['id']
                        orgc.name = org['Organisation']['name']
                        orgc.uuid = org['Organisation']['uuid']
                        event.orgc = orgc
                        break
                
                # if orgc doesn't exist yet
                if not getattr(event, 'orgc', None):
                    orgc = MISPOrganisation()
                    orgc.name = self.rule['defaults']['provider']
                    orgc_resp = self.client.add_organisation(orgc)
                    orgc = orgc.from_dict(**orgc_resp['Organisation'])
                    event.orgc = orgc

        return event

    def _cif_i_to_misp_attr(self, cif_i):
        '''map fields of a single CIF indicator to those of a MISP attribute
        input: cif Indicator() object
        output: MISPAttribute() object
        field conversions:
            indicator -> value
            firsttime -> first_seen
            lasttime -> last_seen
            description -> comment
            itype -> type
            tags -> ...?
        '''
        misp_indicator = MISPAttribute()
        misp_indicator.value = cif_i.indicator
        misp_indicator.first_seen = cif_i.firsttime
        misp_indicator.last_seen = cif_i.lasttime
        misp_indicator.comment = cif_i.description

        if cif_i.tags:
            for tag in cif_i.tags:
                misp_indicator.add_tag(tag)

        # check for rule- or feed-wide static type
        if not self.misp_attrib_type:
            cif_itype = cif_i.itype
            misp_type = CIF_TO_MISP_TYPES[cif_itype]
        misp_indicator.type = self.misp_attrib_type or misp_type

        for setting, value in self.attribute.items():
            setattr(misp_indicator, setting, value)

        # MISP enables "to_ids" if not specified - prevent that
        if not getattr(misp_indicator, 'to_ids', None):
            misp_indicator.to_ids = False

        return misp_indicator

    def indicators_create(self, data, **kwargs):
        self._load_misp_settings_from_smrt_rule(**kwargs)
        misp_event = self._get_misp_event()

        if not isinstance(data, list):
            data = [data]

        for x in data:
            try:
                misp_indicator = self._cif_i_to_misp_attr(x)
            except InvalidIndicator:
                continue
            
            misp_event.add_attribute(**misp_indicator)

        if self.new_event:
            event = self.client.add_event(misp_event)
        else:
            event = self.client.update_event(misp_event)
        if event.get('errors'):
            raise Exception(event['errors'])

Plugin = MISP
