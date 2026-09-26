from __future__ import annotations

import scrapy


class PageItem(scrapy.Item):
    url = scrapy.Field()
    final_url = scrapy.Field()
    seed_id = scrapy.Field()
    depth = scrapy.Field()
    parent_url = scrapy.Field()
    http_status = scrapy.Field()
    content_type = scrapy.Field()
    title = scrapy.Field()
    text = scrapy.Field()
    language = scrapy.Field()
    text_bytes = scrapy.Field()
    outlinks_total = scrapy.Field()
    outlinks_in_scope = scrapy.Field()
    topical_hits = scrapy.Field()
    topical_density = scrapy.Field()
    scope_rule = scrapy.Field()
    truncated = scrapy.Field()
    fetch_ms = scrapy.Field()
