# Content licenses

PrepperPi ships **no content**. It ships a downloader that fetches content from its original source at install time. This document lists the sources the default bundles point at and summarizes each source's license so that operators can make informed choices, especially if they plan to redistribute a pre-loaded PrepperPi.

**Nothing in this document is legal advice.** If you are doing anything beyond personal use, read the actual license on every source before you redistribute.

## Short version

| Source | What it is | License | Attribution required? | Commercial redistribution? |
|---|---|---|---|---|
| Wikipedia (all languages) | Encyclopedia ZIMs via Kiwix | CC BY-SA 4.0 | Yes | Yes, under same license |
| Wikiversity, Wiktionary | Sister projects via Kiwix | CC BY-SA 4.0 | Yes | Yes, under same license |
| Project Gutenberg | Public-domain books via Kiwix | Public domain (most) | Recommended | Yes |
| Khan Academy (Lite) | Offline education ZIM via Kiwix | CC BY-NC-SA 3.0 | Yes | **No** — non-commercial only |
| iFixit | Repair guides ZIM via Kiwix | CC BY-NC-SA 3.0 | Yes | **No** — non-commercial only |
| WikiHow | How-to ZIM via Kiwix | CC BY-NC-SA 3.0 | Yes | **No** — non-commercial only |
| Stack Exchange | Q&A ZIMs via Kiwix | CC BY-SA 4.0 | Yes | Yes, under same license |
| TED talks | Video ZIM via Kiwix | CC BY-NC-ND 4.0 | Yes | **No** — non-commercial, no derivatives |
| WikiMed | Medical ZIM via Kiwix | CC BY-SA 4.0 | Yes | Yes, under same license |
| MedlinePlus | NIH public health info | Public domain (US Gov) | Recommended | Yes |
| US Army / DoD field manuals | e.g. FM 21-76, FM 4-25.11 | Public domain (US Gov) | Recommended | Yes |
| Ready.gov / FEMA | US emergency-preparedness content | Public domain (US Gov) | Recommended | Yes |
| Nuclear War Survival Skills | OISM-published survival guide | Author released to public domain | Recommended | Yes |
| OpenStreetMap / OpenMapTiles | Vector map tiles (MBTiles) | ODbL 1.0 (data) + CC BY 4.0 (style) | Yes | Yes, with attribution and open derivatives |
| Geofabrik extracts | Regional OSM extracts | ODbL 1.0 | Yes | Yes, with attribution and open derivatives |

## The three "NC" sources to watch

Khan Academy Lite, iFixit, and WikiHow are all **non-commercial** licenses. In practice:

- Loading them onto your own PrepperPi for your own household or for a classroom or relief deployment you run: fine.
- Building a batch of PrepperPis and *selling* them pre-loaded with these bundles: **not** fine without separate commercial permission from each publisher.

The admin console will flag any commercial-use-restricted item at install time.

## Source-by-source notes

### Kiwix (the ZIM ecosystem)

Kiwix is not itself a content license; it is the format and the serving infrastructure. The license on each ZIM is whatever the underlying content carries. Kiwix publishes the upstream license on the [Kiwix library](https://library.kiwix.org/) metadata for each file; the PrepperPi updater surfaces that metadata in the admin console.

### US Government works

Works of the US federal government prepared by federal employees in their official capacity are public domain in the United States (17 U.S.C. §105). This covers FEMA, Ready.gov, NIH/MedlinePlus, and US military doctrine publications (field manuals, technical manuals). This does **not** necessarily cover contractor-produced works or third-party material the US Government has licensed. When in doubt, check the front matter of the specific document.

### OpenStreetMap / OpenMapTiles

The raw geographic *data* is licensed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). The default visual *style* used by OpenMapTiles is CC BY 4.0. Any derived tileset or map image you redistribute must carry OpenStreetMap attribution — commonly `© OpenStreetMap contributors`. If you remix the data, derivatives must be open under ODbL.

### Nuclear War Survival Skills (Cresson H. Kearny)

Kearny and the Oregon Institute of Science and Medicine published this work "free to be copied and distributed." The text is widely treated as public domain; original figures are also freely redistributable under the same terms. Attribution to Kearny is customary.

## Adding a new source

If you want a new source included in a default bundle, open an issue that documents:

1. The upstream URL the updater should fetch from.
2. The license text (link and copy).
3. Any attribution required.
4. Whether commercial redistribution is permitted.
5. The approximate size on disk.

A maintainer will either add it to a bundle, mark it as opt-in only, or explain why it can't ship. If the license is unclear, we err on the side of not shipping the pointer.
