"""A smart collection's saved filter: the grid's filters at the time it was saved, which
select the photos again whenever it is opened, so it updates itself.

Stored as JSON in `collections.query` with a version; a field it lacks means any.
"""

import json
from dataclasses import asdict, dataclass, fields

from lumina.core import keywords as keywordm
from lumina.core.dbtypes import Filter

VERSION = 1


@dataclass(frozen=True)
class SmartQuery:
    year: str | None = None
    model: str | None = None
    lens: str | None = None
    # 1 for a thumbs up, 0 for none, -1 for a thumbs down
    flag: int | None = None
    # 0 for no stars, else that many stars or more
    rating: int | None = None
    # "edited" or "unedited"
    edit: str | None = None
    # A keyword path: the photos given it or one below it
    keyword: str | None = None
    # A search text: the photos that match it, best first
    text: str | None = None

    def to_json(self):
        data = {key: value for key, value in asdict(self).items() if value is not None}
        return json.dumps({"version": VERSION, **data}, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        names = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    def to_filter(self):
        """The part the database selects: year, camera and lens."""
        return Filter(year=self.year, model=self.model, lens=self.lens)

    def is_empty(self):
        return not any(getattr(self, field.name) is not None for field in fields(self))

    def passes(self, photo, keyword_paths):
        """Whether `photo` (a PhotoRow) is picked by the part that is not the database's:
        flag, stars, edit and keyword. `keyword_paths` is {keyword id: path}. The search
        text is not tested here: it ranks."""
        if self.flag is not None and photo.flag != self.flag:
            return False
        if self.rating is not None:
            if self.rating == 0 and photo.rating != 0:
                return False
            if self.rating and photo.rating < self.rating:
                return False
        if self.edit is not None and (photo.edited is not None) != (self.edit == "edited"):
            return False
        if self.keyword is not None:
            return any(
                keywordm.within(keyword_paths[k], self.keyword)
                for k in photo.keywords
                if k in keyword_paths
            )
        return True
