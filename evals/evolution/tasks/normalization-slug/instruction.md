Fix `slug.slugify()` for ASCII labels: lowercase the text, replace every run of non-alphanumeric characters with one hyphen, and remove leading or trailing hyphens. Do not modify tests.
