# coding=utf-8


__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"


import logging
import threading
import netifaces
from flask import jsonify, make_response
from flask_babel import gettext

import octoprint.plugin
from octoprint.server import admin_permission
from octoprint_netconnectd.analytics import Analytics


class NetconnectdSettingsPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.AssetPlugin,
):

    LOG_STATE_DELAY = 15.0

    def __init__(self):
        self.address = None
        self._analytics = None

    def initialize(self):
        self._analytics = Analytics(self)
        self.address = self._settings.get(["socket"])
        self.forwardUrl = self._settings.get(["forwardUrl"])
        self.country = self._settings.get(["country"])
        self._log_state_timed(self.LOG_STATE_DELAY)

    @property
    def hostname(self):
        hostname = self._settings.get(["hostname"])
        if hostname:
            return hostname
        else:
            import socket

            return socket.gethostname() + ".local"

    ##~~ SettingsPlugin

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.address = self._settings.get(["socket"])

    def get_settings_defaults(self):
        return dict(
            socket="/var/run/netconnectd.sock",
            hostname=None,
            forwardUrl="http://find.mr-beam.org",
            timeout=80,
            country=None,
        )

    ##~~ TemplatePlugin API

    def get_template_configs(self):
        return [dict(type="settings", name=gettext("Network Connection"))]

    ##~~ SimpleApiPlugin API

    def get_api_commands(self):
        return dict(
            start_ap=[],
            stop_ap=[],
            refresh_wifi=[],
            configure_wifi=[],
            forget_wifi=[],
            reset=[],
            set_country=[],
        )

    def is_api_adminonly(self):
        return False

    def on_api_get(self, request):
        try:
            status = self._get_status()
            if status["wifi"]["present"]:
                wifis = self._get_wifi_list()
            else:
                wifis = []
        except Exception as e:
            return jsonify(dict(error=str(e)))

        try:
            data = self._get_country_list()
            countries = data["countries"]
            country = data["country"]
            self.country = data["country"]
        except Exception as e:
            return jsonify(dict(error=str(e)))

        return jsonify(
            dict(
                wifis=wifis,
                status=status,
                hostname=self.hostname,
                forwardUrl=self.forwardUrl,
                ip_addresses=dict(
                    eth0=self._get_ip_address("eth0"),
                    wlan0=self._get_ip_address("wlan0"),
                ),
                country=country,
                countries=countries,
            )
        )

    def on_api_command(self, command, data, adminRequired=True):
        try:
            if command == "refresh_wifi":
                self._analytics.write_wifi_config_command(command, success=True)
                return jsonify(self._get_wifi_list(force=True))

            # any commands processed after this check require admin permissions
            if adminRequired and not admin_permission.can():
                self._analytics.write_wifi_config_command(
                    command, success=False, err="Insufficient rights"
                )
                return make_response("Insufficient rights", 403)

            if command == "configure_wifi":
                if data["psk"]:
                    self._logger.info(
                        "Configuring wifi {ssid} and psk...".format(**data)
                    )
                else:
                    self._logger.info("Configuring wifi {ssid}...".format(**data))

                self._configure_and_select_wifi(
                    data["ssid"],
                    data["psk"],
                    force=data["force"] if "force" in data else False,
                )

            elif command == "forget_wifi":
                self._forget_wifi()

            elif command == "reset":
                self._reset()

            elif command == "start_ap":
                self._start_ap()

            elif command == "stop_ap":
                self._stop_ap()

            elif command == "set_country":
                self._set_country(data["country"])

            self._analytics.write_wifi_config_command(command, success=True)

        except RuntimeError as e:
            self._analytics.write_wifi_config_command(
                command, success=False, err=str(e)
            )
            raise RuntimeError

    ##~~ AssetPlugin API

    def get_assets(self):
        return dict(
            js=["js/netconnectd.js"],
            css=["css/netconnectd.css"],
            less=["less/netconnectd.less"],
        )

    ##~~ Private helpers

    def _get_wifi_list(self, force=False):
        payload = dict()
        if force:
            self._logger.info("Forcing wifi refresh...")
            payload["force"] = True

        flag, content = self._send_message("list_wifi", payload)
        if not flag:
            raise RuntimeError("Error while listing wifi: " + content)

        result = []
        for wifi in content:
            result.append(
                dict(
                    ssid=wifi["ssid"],
                    address=wifi["address"],
                    quality=wifi["signal"],
                    encrypted=wifi["encrypted"],
                )
            )
        return result

    def _get_country_list(self, force=False):
        payload = {}

        try:
            # The "country_list" call only exists in the netconnectd server of the new image and not the old one
            flag, content = self._send_message("country_list", payload)
            if not flag:
                raise RuntimeError("Error while getting countries wifi: " + content)

            countries = []
            for country in content["countries"]:
                countries.append(country)
            return {"country": content["country"], "countries": countries}

        except Exception as e:
            output = "Error while getting countries wifi: {}".format(e)
            self._logger.warn(output)
            return {"country": "", "countries": []}

    def _get_status(self):
        payload = dict()

        flag, content = self._send_message("status", payload)
        if not flag:
            raise RuntimeError("Error while querying status: " + content)

        return content

    def _configure_and_select_wifi(self, ssid, psk, force=False):
        payload = dict(ssid=ssid, psk=psk, force=force)

        flag, content = self._send_message("config_wifi", payload)
        self._logger.info("config_wifi: flag=%s, content=%s", flag, content)
        if not flag:
            raise RuntimeError("Error while configuring wifi: " + content)

        flag, content = self._send_message("start_wifi", dict())
        self._logger.info("start_wifi: flag=%s, content=%s", flag, content)
        if not flag:
            raise RuntimeError("Error while selecting wifi: " + content)

    def _forget_wifi(self):
        payload = dict()
        flag, content = self._send_message("forget_wifi", payload)
        if not flag:
            raise RuntimeError("Error while forgetting wifi: " + content)

    def _reset(self):
        payload = dict()
        flag, content = self._send_message("reset", payload)
        if not flag:
            raise RuntimeError("Error while factory resetting netconnectd: " + content)

    def _start_ap(self):
        payload = dict()
        flag, content = self._send_message("start_ap", payload)
        if not flag:
            raise RuntimeError("Error while starting ap: " + content)

    def _stop_ap(self):
        payload = dict()
        flag, content = self._send_message("stop_ap", payload)
        if not flag:
            raise RuntimeError("Error while stopping ap: " + content)

    def _set_country(self, country_code):
        payload = {"country_code": country_code}
        # The "set_country" call only exists in the netconnectd server of the new image and not the old one
        flag, content = self._send_message("set_country", payload)
        if not flag:
            raise RuntimeError("Error while setting country: " + content)

    def _send_message(self, message, data):
        obj = dict()
        obj[message] = data

        import json

        js = json.dumps(obj, separators=(",", ":"))

        import socket

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._settings.get_int(["timeout"]))
        try:
            sock.connect(self.address)
            sock.sendall(js + "\x00")

            buffer = []
            while True:
                chunk = sock.recv(16)
                if chunk:
                    buffer.append(chunk)
                    if chunk.endswith("\x00"):
                        break

            data = "".join(buffer).strip()[:-1]

            response = json.loads(data.strip())
            if "result" in response:
                return True, response["result"]

            elif "error" in response:
                # something went wrong
                self._logger.warn(
                    "Request to netconnectd went wrong: " + response["error"]
                )
                return False, response["error"]

            else:
                output = "Unknown response from netconnectd: {response!r}".format(
                    response=response
                )
                self._logger.warn(output)
                return False, output

        except Exception as e:
            output = "Error while talking to netconnectd: {}".format(e)
            self._logger.warn(output)
            return False, output

        finally:
            sock.close()

    def _get_ip_address(self, interface):
        """
        Returns the external IP address of the given interface
        :param interface:
        :return: String IP
        """
        try:
            res = []
            for tmp in netifaces.ifaddresses(interface)[netifaces.AF_INET]:
                res.append(tmp["addr"])
            return ", ".join(res)
        except:
            pass

    def _log_state_timed(self, delay=0):
        if delay > 0:
            myThread = threading.Timer(delay, self._log_state_timed)
            myThread.daemon = True
            myThread.name = "Netconnectd_log_state_timer"
            myThread.start()
        else:
            msg = "Netconnectd status: ip_eth0: {}, ip_wlan0: {}, status: {}".format(
                self._get_ip_address("eth0"),
                self._get_ip_address("wlan0"),
                self._get_status(),
            )
            logging.getLogger("octoprint.plugins." + __name__).info(msg)


__plugin_name__ = "Netconnectd Client"
__plugin_pythoncompat__ = ">=2.7,<4"


def __plugin_check__():
    import sys

    if sys.platform.startswith("linux"):
        return True

    logging.getLogger("octoprint.plugins." + __name__).warn(
        "The netconnectd plugin only supports Linux"
    )
    return False


def __plugin_load__():
    # since we depend on a Linux environment, we instantiate the plugin implementation here since this will only be
    # called if the OS check above was successful
    global __plugin_implementation__
    __plugin_implementation__ = NetconnectdSettingsPlugin()
    return True
