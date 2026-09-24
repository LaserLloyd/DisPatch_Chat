# ui-theme — drop-in theme folder

Theme revision 2026-09-24. Generated files: never edit a copy. To update the themes, replace
this whole folder with a newer one; nothing else in the app needs to change.

## Plugging it into a page

Copy this folder into the app's static files, then put these tags in `<head>`, in this order:

```html
<script src="ui-theme/ui-theme.js"
        data-themes="purple,midnight-gold,glacier,forest,paper,daylight"
        data-default="midnight-gold"
        data-storage-key="myapp.theme"></script>
<link rel="stylesheet" href="ui-theme/ui-theme-base.css">
<!-- the app's own stylesheets -->
<link rel="stylesheet" href="ui-theme/ui-theme.css">
```

- `data-themes`: the themes the app's picker offers, in picker order. Opt-in themes (for
  example `night-red`) appear only when listed.
- `data-default`: the theme a new visitor gets.
- `data-storage-key`: the localStorage key that remembers the choice.
- Optional: `data-legacy-key` and `data-legacy-map` (`old:new,old:new`) migrate an older
  setting once; `data-families="true"` enables the dark/light family toggle.

`ui-theme-base.css` is the element layer (body type, focus ring, scrollbars, the text
standard). An app that keeps its own element styles may leave it out.

Adapters, each linked right after the file it extends:

| Adapter | For | After |
|---|---|---|
| `adapters/quasar.js` | NiceGUI / Quasar: dark mode follows the theme | `ui-theme.js` |
| `adapters/quasar.css` | NiceGUI / Quasar: tokens onto Quasar components | `ui-theme.css` |
| `adapters/dispatch-compat.css` | DisPatch's own token names (`--bg-*`, `--user-bubble`, `--md-*`, ...) | `ui-theme.css` |
| `adapters/clawchat-compat.css` | ClawChat's legacy token names | `ui-theme.css` |

Add `<select data-ui-theme-picker aria-label="Theme"></select>` where the picker belongs, or
call `UITheme.mountPicker(selectElement)` for a picker created after the page loaded.

Any element may carry its own `data-palette="<slug>"` (a theme preview card, say): the tokens
are scoped to `:root, [data-palette]`, so it renders in that theme.

## Caching

Serve these files so a new copy is picked up: either revalidate on every load
(`Cache-Control: no-cache`, or `max-age=0`), or version the URLs from the file contents at
request time. `VERSION` changes whenever any file here changes.
