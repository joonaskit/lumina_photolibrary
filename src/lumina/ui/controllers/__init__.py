"""The window's parts: a controller per page and per feature, each with the widgets and
services it works with handed to it. They talk through the services and the `Navigator`
(which page shows, which photo is looked at, which photos are chosen), not through the
window, which builds the widgets and the actions and connects them to these (#108)."""
