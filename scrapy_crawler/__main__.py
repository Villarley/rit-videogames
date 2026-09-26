"""Allow python -m scrapy_crawler as an alias for run."""

from scrapy_crawler.run import main

raise SystemExit(main())
