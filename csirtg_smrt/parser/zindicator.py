from csirtg_smrt.parser import Parser
from pprint import pprint


class _Indicator(Parser):
    def __init__(self, *args, **kwargs):
        super(_Indicator, self).__init__(*args, **kwargs)

    def process(self):
        defaults = self._defaults()

        for l in self.fetcher.process():
            for e in l:
                i = {} # will become copy of Indicator() dict with rule defaults copied over
                e = e.__dict__() # Indicator() obj as dict, basically deepcopying e into i, then
                # overwriting any i values with things specified in smrt rule defaults section
                for k, v in e.items():
                    i[k] = v

                for k, v in defaults.items():
                    i[k] = v

                self.eval_obs(i, original_indicator=e)
                yield i


Plugin = _Indicator
