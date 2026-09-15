# Operational Event Dispatcher

ROS 2 bridge between `/fleet/operational_events` and the
`/fleet/deployment_request` Action server.

Only `STATE_ENTER` messages for the three demo event types are dispatched.
Policy selection and concrete remediation remain owned by the Application
Manager. Recovery events stay on the topic and are used by the manager to
verify the requested outcome.
