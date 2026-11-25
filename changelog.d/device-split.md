### Added

- In a group whose agents split their devices (Floorbit #1441), an agent logs in only to its own
  devices, and its syslog relay sends only their lines; the rest it keeps a minute and sends when
  a device becomes its own after a failover.
