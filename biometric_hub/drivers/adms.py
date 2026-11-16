"""Push-mode ZKTeco readers.

There is nothing to poll: the device connects to us. The driver exists so a
device can be configured against a named driver like any other, and so the
registry records what push mode can and cannot do.
"""

from biometric_hub.drivers.base import BaseDriver


class ADMSDriver(BaseDriver):
	driver_name = "ZKTeco Push (ADMS)"
	driver_version = "1.0.0"
	supports_push = True
	supports_realtime = True

	def test_connection(self, device):
		if not device.serial_no:
			return False, "Set the device's Serial Number — in push mode it is the only way it identifies itself."
		if not device.last_contact:
			return False, (
				"This device has never connected. Point it at this site's /iclock endpoint "
				"and check it can reach the server outbound."
			)
		return True, f"Last heard from at {device.last_contact}."

	def parse_push(self, device, request):
		from biometric_hub.api.adms import parse_attlog

		body = request.get_data(as_text=True) if hasattr(request, "get_data") else str(request)
		return parse_attlog(device.serial_no, body)
