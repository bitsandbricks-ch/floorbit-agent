### Added

- The front door can be published on other ports than 443 and 80: set `HTTPS_PUBLISH_PORT` and
  `HTTP_PUBLISH_PORT` in compose's `.env`. The agent hears them from compose, sends browsers on to the
  right HTTPS port and tells Floorbit, so the seat walk-through names the right address.
- `FLOORBIT_AGENT_HALL_URL` (or "Reached in the hall at" on the agent's page in Floorbit) names where
  laptops in the hall reach the agent, for a reverse proxy or port forwarding.
