"""Values shared across the app that are not tied to one module."""

# the one central release source of the retail till (crenya pos): a static, signed-artifact
# manifest (README, *Till updates*). Site config `CENTRAL_RELEASES_CONFIG` overrides the URL;
# an empty value turns the central source off.
CENTRAL_RELEASES_URL = "https://dl.crenya.com/pos/releases.json"
CENTRAL_RELEASES_CONFIG = "crenya_pos_releases_url"
