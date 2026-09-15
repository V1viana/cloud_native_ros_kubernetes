uint8 SEVERITY_INFO=0
uint8 SEVERITY_WARNING=1
uint8 SEVERITY_CRITICAL=2

uint8 STATE_ENTER=0
uint8 STATE_ACTIVE=1
uint8 STATE_RECOVERED=2
uint8 STATE_FAILED=3

std_msgs/Header header
string event_id
string correlation_id
string source
string robot_id
string component
string event_type
uint8 severity
uint8 state
float64 observed_value
float64 threshold
float64 window_sec
diagnostic_msgs/KeyValue[] attributes
