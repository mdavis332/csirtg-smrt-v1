'''
MISP client used to send fetched/parsed indicators _to_ a MISP instance
cli launch should look like the following:
> csirtg-smrt --skip-invalid --client misp --rule ~/push_to_misp.yml --remote http://<misp> \
  --token <MISP_write_token>

The MISP API token must have a role with the following permissions:
  - Auth key access (to use the API)
  - Tagger (to attach tags)
  - Tag Editor (to create new tags if necessary)
  - Site Admin (only if you want creator org spoofing -- see below)

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
        uuid: eca64394-2895-65ac-475a-b4f81ba41698
        published: 1

If no destination event uuid field is specified in the rule, then a new event will be created in MISP
  for every fireball batch (default every 100 indicators). The 'uuid' field under event will also accept
  the following human datetime strings: hour, day, week, month, or permanent. If used, the resultant 
  timestamp (beginning of the hour, day, week, or month) will be used to create the same deterministic 
  uuidv4. In this way, specifying 'day' for uuid will result in all those indicators from each smrt run 
  going into the same MISP event for that day. Specifying 'week' will ensure all indicators go in the same
  event for that week, etc. If 'permanent' is specified, a uuid is deterministically created from the
  provider and feed name, so the target MISP UUID should always be the same unless those change.

If you want to spoof the "creator org" field of the MISP event, you can specify an arbitrary str or 
  reuse the 'provider' field by employing the 'eval()' func in the event dict of the yml rule under
  orgc_id. e.g.,
  remote: http://danger.rulez.sk/projects/bruteforceblocker/blist.php
  defaults:
    provider: danger.rulez.sk
    reference: http://danger.rulez.sk/projects/bruteforceblocker/blist.php
    tlp: clear
    to_misp:
      event:
        orgc_id: eval("{provider}".format(**obs))
        tags:
            - bruteforce

  Doing the above requires the MISP API token to have Site Admin permissions so it can create new Orgs.
  In the case above, the client will see if "danger.rulez.sk" is already an Org listed on the target MISP
  instance. If so, it will use the existing Org ID when creating the event. If not, it will create a new
  org named "danger.rulez.sk" (or whatever's in the provider field), and set that on the event.

'''

import logging

from pymisp import PyMISP, MISPEvent, MISPAttribute, MISPOrganisation
from csirtg_indicator.exceptions import InvalidIndicator
from csirtg_indicator.utils.ztime import parse_timestamp
from csirtg_smrt.client.plugin import Client
from hashlib import md5
from uuid import UUID

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

HUMAN_TS = {'hour', 'day', 'week', 'month', 'permanent'}

class MISP(Client):

    def __init__(self, remote, token, **kwargs):

        super().__init__(remote, token)
        self.verify_ssl = kwargs.get('verify_ssl', True)
        debug = False
        if logger.level == logging.DEBUG:
            debug = True
        self.client = PyMISP(remote, token, self.verify_ssl, debug=debug, tool='csirtg-smrt')
        
    def start(self):
        return True

    def stop(self):
        return True

    def _load_misp_settings_from_smrt_rule(self, **kwargs):
        self.rule = dict(kwargs['rule'].items())
        # handle any 'eval's in the rule
        self.rule['defaults'] = kwargs['parser'].eval_obs(self.rule.get('defaults', {}))
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

    def _set_event_id(self, event_id=None):
        if not event_id:
            return -1
        
        # always return same UUIDv4 for same timestamp
        if event_id in HUMAN_TS:
            if event_id != 'permanent':
                event_id = str(UUID(md5(str(parse_timestamp(event_id)).encode('utf-8')).hexdigest(), version=4))
            else:
                # arbitrarily pick semi-static fields and order to create a hash/uuid
                # in this case, we use the format provider:feed_name, e.g., nixspam.net:ssh
                hash_candidate = '{}:{}'.format(self.rule['defaults']['provider'], self.feed)
                event_id = str(UUID(md5(hash_candidate)))
            self.event['uuid'] = event_id

        # ensure valid UUID
        try:
            _ = isinstance(UUID(event_id), UUID)
        except ValueError:
            return -1

        return event_id
    
    def _get_misp_event(self):
        '''fetches the MISP event to which attributes should be added. if one is not specified or
        doesn't exist, a new event is created.
        input: None
        output: MISPEvent() object
        '''
        dst_event_id = self._set_event_id(self.event.get('uuid'))
        self.new_event = False
        # do this to suppress error if event not found
        log_level = logging.getLogger('pymisp').level
        logging.getLogger('pymisp').setLevel(logging.CRITICAL)
        event = self.client.get_event(dst_event_id, pythonify=True)
        logging.getLogger('pymisp').setLevel(log_level)
        
        # if event doesn't exist, create new one
        if event.get('errors'):
            if event.get('errors', tuple())[0] == 404:
                event = MISPEvent()
                self.new_event = True
            else:
                raise Exception(event)

        for setting, value in self.event.items():
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
                event.add_tag('source:{}'.format(reference))

            # in MISP, it's customary to designate TLP at Event level rather than Attrib level
            tlp = self.rule.get('defaults', {}).get('tlp', 'amber')
            event.add_tag('tlp:{}'.format(tlp))

            # handle spoofing a provided name
            if getattr(event, 'orgc_id', None) and not isinstance(event.orgc_id, int):
                provider = event.orgc_id
                event.orgc_id = None
                # Set orgc from provider field
                # does orgc already exist?
                instance_orgs = self.client.organisations()
                for org in instance_orgs:
                    if org['Organisation']['name'] == provider:
                        orgc = MISPOrganisation()
                        orgc.id = org['Organisation']['id']
                        orgc.name = org['Organisation']['name']
                        orgc.uuid = org['Organisation']['uuid']
                        event.orgc = orgc
                        break
                
                # if orgc doesn't exist yet
                if not getattr(event, 'orgc', None):
                    orgc = MISPOrganisation()
                    orgc.name = provider
                    orgc.description = 'created by csirtg-smrt'
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
        misp_indicator.comment = '|'.join([cif_i.description, cif_i.reference]) if cif_i.reference else cif_i.description

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
