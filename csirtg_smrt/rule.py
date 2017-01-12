import yaml
import json
import logging
from csirtg_smrt.exceptions import RuleUnsupported


class Rule(dict):

    def __init__(self, path=None, rule=None, **kwargs):
        self.logger = logging.getLogger(__name__)
        if path:
            if path.endswith('.yml'):
                with open(path) as f:
                    try:
                        d = yaml.load(f)
                    except Exception as e:
                        self.logger.error('unable to parse {0}'.format(path))
                        raise RuntimeError(e)

                self.defaults = d.get('defaults')
                self.feeds = d.get('feeds')
                self.parser = d.get('parser')
                self.fetcher = d.get('fetcher')
                self.skip = d.get('skip')
                self.skip_first = d.get('skip_first')
                self.remote = d.get('remote')
                self.replace = d.get('replace')
                self.itype = d.get('itype')
                self.remote_pattern = d.get('remote_pattern')
            else:
                raise RuleUnsupported('unsupported file type: {}'.format(path))
        else:
            self.defaults = rule.get('defaults')
            self.feeds = rule.get('feeds')
            self.parser = rule.get('parser')
            self.fetcher = rule.get('fetcher')
            self.skip = rule.get('skip')
            self.skip_first = rule.get('skip_first')
            self.remote = rule.get('remote')
            self.replace = rule.get('replace')
            self.itype = rule.get('itype')
            self.remote_pattern = rule.get('remote_pattern')

    def __repr__(self):
        return json.dumps({
            "defaults": self.defaults,
            "feeds": self.feeds,
            "parser": self.parser,
            "fetcher": self.fetcher,
            'skip': self.skip,
            "skip_first": self.skip_first,
            'remote': self.remote,
            'remote_pattern': self.remote_pattern,
            'replace': self.replace,
            'itype': self.itype,
        }, sort_keys=True, indent=4, separators=(',', ': '))
