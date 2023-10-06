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

  The 'uuid' field under event will also accept a string starting with 'group_by:' and ending with any
  CIF Indicator() field like 'reference.' e.g., uuid: 'group_by:reference'
  Whatever field comes after the 'group_by:' trigger (in this example, 'reference') will cause the same
  deterministic UUID to be created from the _value_ of that field, ensuring that any indicators with that
  same field value will always go to the same MISP event. e.g., if the 'reference' field contains a unique 
  report value like 'http://tipfeed.tld/reports/001a,' then all indicators with that same URL will wind up
  in the same MISP Event, and each different report URL will have its own unique MISP Event. This is useful
  when you want to create a MISPEvent() for each unique report and ensure all indicators (MISP attributes)
  associated with that report wind up in the same MISP event. Warning: group_by is 12x slower due to 
  needing to iterate the indicator list multiple times for sorting.

The trigger 'evaluated_indicator_field:' can be used to add the info from the given field in the first
  indicator of a batch. e.g., evaluated_indicator_field:additional_data
The special attribute "report" will be used to add that info to an event report within the MISP event.
  Using the above evaluated_indicator_field trigger, a report can be added to the MISP event containing
  the text from the additional_data field using something like the below:
  defaults:
    to_misp:
      event:
        uuid: group_by:reference
        info: evaluated_indicator_field:description
        report: evaluated_indicator_field:additional_data

If you want to spoof the "creator org" field of the MISP event, you can specify an arbitrary str like 
  the 'provider' in the event dict of the yml rule under
  orgc_id. e.g.,
  remote: http://danger.rulez.sk/projects/bruteforceblocker/blist.php
  defaults:
    provider: danger.rulez.sk
    reference: http://danger.rulez.sk/projects/bruteforceblocker/blist.php
    tlp: clear
    to_misp:
      event:
        orgc_id: danger.rulez.sk
        tags:
            - bruteforce

  Doing the above requires the MISP API token to have Site Admin permissions so it can create new Orgs.
  In the case above, the client will see if "danger.rulez.sk" is already an Org listed on the target MISP
  instance. If so, it will use the existing Org ID when creating the event. If not, it will create a new
  org named "danger.rulez.sk" (or whatever's in the provider field), and set that on the event.

'''

import logging

from pymisp import PyMISP, MISPEvent, MISPAttribute, MISPOrganisation
from csirtg_indicator.utils.ztime import parse_timestamp
from csirtg_smrt.client.plugin import Client
from collections import defaultdict
from operator import attrgetter
from hashlib import md5
from uuid import UUID

logger = logging.getLogger(__name__)

CIF_TO_MISP_TYPES = {
    'ipv4': 'ip-src',
    'ipv6': 'ip-src',
    'asn': 'AS',
    'fqdn': 'hostname',
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
REPORT_NAME = 'from_smrt'

# itertools.groupby requires a sorted list, so implement something custom for unsorted
def groupby(seq, key=lambda x: x):
    indices = defaultdict(list)
    for i, elem in enumerate(seq):
        #indices[key(elem)].append(i)
        indices[key(elem)].append(elem)
    return indices
    # in tests, generators took slightly longer
    #for k, idxs in indices.items():
    #    yield k, (seq[i] for i in idxs)

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
        #self.rule['defaults'] = kwargs['parser'].eval_obs(self.rule.get('defaults', {}))
        self.event = {}
        self.attribute = {}
        self.feed = kwargs.get('feed', '')
        self.feed_defaults = self.rule['feeds'][self.feed].get('defaults', {})
        self.rule_defaults = self.rule.get('defaults', {})
        self.remote = self.rule['feeds'][self.feed].get('remote') or self.rule.get('remote')
        rule_event = self.rule_defaults.get(SMRT_RULE_MISP_TOPLEVEL, {}).get('event', {})
        feed_event = self.feed_defaults.get(SMRT_RULE_MISP_TOPLEVEL, {}).get('event', {})
        rule_attr = self.rule_defaults.get(SMRT_RULE_MISP_TOPLEVEL, {}).get('attribute', {})
        feed_attr = self.feed_defaults.get(SMRT_RULE_MISP_TOPLEVEL, {}).get('attribute', {})
        # merge 2 dicts giving priority to kv's in last merged (feed-level)
        self.event = rule_event | feed_event
        self.attribute = rule_attr | feed_attr

        # if whole feed is same misp attrib type, allow manual input and don't resolve each
        self.misp_attrib_type = self.rule['feeds'][self.feed].get('itype') or \
           self.feed_defaults.get('itype') or self.rule.get('itype') or self.rule_defaults.get('itype')

        # everything with same group_by field goes into same MISP Event
        # good for grouping indicators from a report if 'reference' field set to report URL like
        # https://source.tld/reports/reportId504
        self.group_by = self.event.get('uuid')[9:] if \
            str(self.event.get('uuid')).startswith('group_by:') else None
        
        self.event_field_from_evald_indicator = []

    def _get_event_uuid(self, event_id=None):
        '''
        input: event_id (str): UUIDv4 or in HUMAN_TS or other str value from group_by field
        output: UUIDv4 (str) or -1 (int)
        '''
        if not event_id:
            return -1
        
        # always return same UUIDv4 for same timestamp
        if event_id in HUMAN_TS:
            if event_id == 'permanent':
                # arbitrarily pick semi-static fields and order to create a hash/uuid
                # in this case, we use the format provider:feed_name, e.g., nixspam.net:ssh
                hash_candidate = '{}:{}'.format(self.rule['defaults']['provider'], self.feed)
                event_id = str(UUID(md5(hash_candidate.encode('utf-8')).hexdigest(), version=4))
            else:
                event_id = str(UUID(md5(str(parse_timestamp(event_id)).encode('utf-8')).hexdigest(), version=4))
        elif self.group_by:
            event_id = str(UUID(md5(event_id.encode('utf-8')).hexdigest(), version=4))

        # ensure valid UUID
        try:
            _ = isinstance(UUID(event_id), UUID)
        except ValueError:
            return -1

        return event_id
    
    def _get_misp_event(self, event_uuid):
        '''fetches the MISP event to which attributes should be added. if one is not specified or
        doesn't exist, a new event is created.
        input: None
        output: MISPEvent() object
        '''
        
        new_event = False
        # do this to suppress error if event not found
        log_level = logging.getLogger('pymisp').level
        logging.getLogger('pymisp').setLevel(logging.CRITICAL)
        misp_event = self.client.get_event(event_uuid, pythonify=True)
        logging.getLogger('pymisp').setLevel(log_level)
        
        # if event doesn't exist, create new one
        if misp_event.get('errors'):
            if misp_event.get('errors', tuple())[0] == 404:
                misp_event = MISPEvent()
                new_event = True
            else:
                raise Exception(misp_event)

        if self.remote:
            misp_event.add_tag('source:{}'.format(self.remote))

        for setting, value in self.event.items():
            if isinstance(value, str) and value.startswith('evaluated_indicator_field:'):
                self.event_field_from_evald_indicator.append((setting, value[26:]))
                continue
            if setting == 'tags':
                tags = value
                if not isinstance(tags, list):
                    tags = [tags]
                for tag in tags:
                    misp_event.add_tag(tag)
                continue
            elif setting == 'uuid':
                misp_event.uuid = event_uuid
                continue
            setattr(misp_event, setting, value)

        if new_event:
            # MISP api contract requires the "info" field
            if not getattr(misp_event, 'info', None):
                misp_event.info = '{}:{}'.format(self.rule['defaults']['provider'], self.feed)

            # reject remote sync edits
            # https://github.com/MISP/PyMISP/issues/239
            misp_event.locked = True

            # in MISP, it's customary to designate TLP at Event level rather than Attrib level
            tlp = self.rule_defaults.get('tlp', 'amber')
            misp_event.add_tag('tlp:{}'.format(tlp))

            # handle spoofing a provided name
            if getattr(misp_event, 'orgc_id', None) and not isinstance(misp_event.orgc_id, int):
                provider = misp_event.orgc_id
                misp_event.orgc_id = None
                # Set orgc from provider field
                # does orgc already exist?
                instance_orgs = self.client.organisations()
                for org in instance_orgs:
                    if org['Organisation']['name'] == provider:
                        orgc = MISPOrganisation()
                        orgc.id = org['Organisation']['id']
                        orgc.name = org['Organisation']['name']
                        orgc.uuid = org['Organisation']['uuid']
                        misp_event.orgc = orgc
                        break
                
                # if orgc doesn't exist yet
                if not getattr(misp_event, 'orgc', None):
                    orgc = MISPOrganisation()
                    orgc.name = provider
                    orgc.description = 'created by csirtg-smrt'
                    orgc_resp = self.client.add_organisation(orgc)
                    orgc = orgc.from_dict(**orgc_resp['Organisation'])
                    misp_event.orgc = orgc

        return (misp_event, new_event)

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
        if cif_i.firsttime:
            misp_indicator.first_seen = cif_i.firsttime
        if cif_i.lasttime:
            misp_indicator.last_seen = cif_i.lasttime
        if not cif_i.description:
            cif_i.description = '' 
        misp_indicator.comment = '|'.join([cif_i.description, cif_i.reference]) if cif_i.reference else cif_i.description

        # check for rule- or feed-wide static type
        if not self.misp_attrib_type:
            misp_type = CIF_TO_MISP_TYPES[cif_i.itype]
            if misp_type == 'md5' and cif_i.tags and 'imphash' in cif_i.tags:
                misp_type = 'imphash'

        if cif_i.portlist:
            if isinstance(cif_i.portlist, int):
                cif_i.portlist = str(cif_i.portlist)
                
            if len(cif_i.portlist.split(',')) == 1 and len(cif_i.portlist.split('-')) == 1:
                misp_indicator.value = '{}|{}'.format(misp_indicator.value, cif_i.portlist)
                if cif_i.itype in ['ipv4', 'ipv6']:
                    misp_type = 'ip-dst|port'
                elif cif_i.itype == 'fqdn':
                    mis_type = 'hostname|port'
        
        misp_indicator.type = self.misp_attrib_type or misp_type

        for setting, value in self.attribute.items():
            setattr(misp_indicator, setting, value)

        # MISP enables "to_ids" if not specified - prevent that
        if not getattr(misp_indicator, 'to_ids', None):
            misp_indicator.to_ids = False

        return misp_indicator

    def indicators_create(self, data, **kwargs):
        if not isinstance(data, list):
            data = [data]

        self._load_misp_settings_from_smrt_rule(**kwargs)

        if self.group_by:
            iter_data = groupby(data, key=attrgetter(self.group_by))
        else:
            # arbitrarily force list of cif indicators (data) into similar format produced by groupby
            # so we can iterate both the same
            iter_data = groupby(data, key=lambda x: 'data')

        for gb_value, v in iter_data.items():
            # one counter for each UUID/MISPEvent
            count = 0
            # get the right MISPEvent once per UUID
            if self.group_by:
                dst_event_uuid = self._get_event_uuid(gb_value)
            else:
                dst_event_uuid = self._get_event_uuid(self.event.get('uuid'))

            misp_event, new_event = self._get_misp_event(dst_event_uuid)
            for cif_i in v:
                if count == 0:
                    # handle adding tags here b/c by now they've been eval'd (if necessary)
                    # only apply i tags 1 time to event b/c they'll all be the same per feed/event
                    if cif_i.tags:
                        for tag in cif_i.tags:
                            misp_event.add_tag(tag)
                    # map additional_data field to MISPEventReport content, but only if not exists
                    #if cif_i.additional_data and not any
                    # apply any evaluated indicator values to the event
                    if self.event_field_from_evald_indicator:
                        for setting, cif_field in self.event_field_from_evald_indicator:
                            value = getattr(cif_i, cif_field)
                            if setting == 'report':
                                if value and not any(
                                    REPORT_NAME in x.name for x in misp_event.event_reports
                                ):
                                    misp_event.add_event_report(name=REPORT_NAME, content=cif_i.additional_data)
                                continue
                            setattr(misp_event, setting, value)
                    count = 1

                misp_indicator = self._cif_i_to_misp_attr(cif_i)
                misp_event.add_attribute(**misp_indicator)

            if new_event:
                event = self.client.add_event(misp_event)
            else:
                event = self.client.update_event(misp_event)
            if event.get('errors'):
                raise Exception(event['errors'])

Plugin = MISP
