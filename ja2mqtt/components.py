# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas@vitvar.com

from __future__ import absolute_import
from __future__ import unicode_literals

import time
import json
import logging
import threading
import re

import serial as py_serial
import paho.mqtt.client as mqtt

from ja2mqtt.utils import (
    Map,
    merge_dicts,
    deep_eval,
    deep_merge,
    PythonExpression,
)
from ja2mqtt.config import Config
from .simulator import Simulator
from queue import Queue


class Component:
    def __init__(self, config, name):
        self.log = logging.getLogger(name)
        self.config = config
        self.name = name

    def worker(self, exit_event):
        pass

    def start(self, exit_event):
        threading.Thread(target=self.worker, args=(exit_event,), daemon=True).start()


class Serial(Component):
    """
    Serial provides an interface for the serial port where JA-121T is connected.
    """

    def __init__(self, config):
        super().__init__(config.get_part("serial"), "serial")
        self.encoding = self.config.value_bool("encoding", default="ascii")
        self.use_simulator = self.config.value_bool("use_simulator", default=False)
        if not self.use_simulator:
            self.ser = py_serial.serial_for_url(
                self.config.value_str("port", required=True), do_not_open=True
            )
            self.ser.baudrate = self.config.value_int("baudrate", min=0, default=9600)
            self.ser.bytesize = self.config.value_int(
                "bytesize", min=7, max=8, default=8
            )
            self.ser.parity = self.config.value_str("parity", default="N")
            self.ser.stopbits = self.config.value_int("stopbits", default=1)
            self.ser.rtscts = self.config.value_bool("rtscts", default=False)
            self.ser.xonxoff = self.config.value_bool("xonxoff", default=False)
            self.log.info(
                f"The serial connection configured, the port is {self.ser.port}"
            )
            self.log.debug(f"The serial object is {self.ser}")
        else:
            self.ser = Simulator(config.get_part("simulator"), self.encoding)
            self.log.info(
                "The simulation is enabled, events will be simulated. The serial interface is not used."
            )
            self.log.debug(f"The simulator object is {self.ser}")
        self.ser.timeout = 1
        self.on_data_ext = None

    def on_data(self, data):
        self.log.debug(f"Received data from serial: {data}")
        if self.on_data_ext is not None:
            self.on_data_ext(data)

    def open(self):
        self.ser.open()

    def close(self):
        self.ser.close()

    def writeline(self, line):
        self.log.debug(f"Writing to serial: {line}")
        try:
            self.ser.write(bytes(line + "\n", self.encoding))
        except Exception as e:
            self.log.error(str(e))

    def worker(self, exit_event):
        self.open()
        try:
            while not exit_event.is_set():
                x = self.ser.readline()
                if x != b"":
                    self.on_data(x.decode(self.encoding).strip("\r\n"))
                exit_event.wait(0.2)
        finally:
            self.close()

    def start(self, exit_event):
        super().start(exit_event)
        if self.use_simulator and isinstance(self.ser, Simulator):
            self.ser.start(exit_event)


class MQTT(Component):
    """
    MQTTClient provides an interface for MQTT broker.
    """

    def __init__(self, name, config):
        super().__init__(config.get_part("mqtt-broker"), "mqtt")
        self.client_name = name
        self.address = self.config.value_str("address")
        self.port = self.config.value_int("port", default=1883)
        self.keepalive = self.config.value_int("keepalive", default=60)
        self.reconnect_after = self.config.value_int("reconnect_after", default=30)
        self.loop_timeout = self.config.value_int("loop_timeout", default=1)
        self.client = None
        self.connected = False
        self.on_connect_ext = None
        self.on_message_ext = None
        self.log.info(f"The MQTT client configured for {self.address}.")
        self.log.debug(f"The MQTT object is {self}.")

    def __str__(self):
        return (
            f"{self.__class__}: name={self.name}, address={self.address}, port={self.port}, keepalive={self.keepalive}, "
            + f"reconnect_after={self.reconnect_after}, loop_timeout={self.loop_timeout}, connected={self.connected}"
        )

    def on_message(self, client, userdata, message):
        if self.on_message_ext is not None:
            try:
                self.on_message_ext(client, userdata, message)
            except Exception as e:
                self.log.error(str(e))

    def on_connect(self, client, userdata, flags, rc):
        self.connected = True
        self.client.on_message = self.on_message
        self.log.info(f"Connected to the MQTT broker at {self.address}:{self.port}")
        if self.on_connect_ext is not None:
            self.on_connect_ext(client, userdata, flags, rc)

    def on_disconnect(self, client, userdata, rc):
        self.log.info(f"Disconnected from the MQTT broker.")
        if rc != 0:
            self.log.error("The client was disconnected unexpectedly.")
        self.connected = False

    def init_client(self):
        self.client = mqtt.Client(self.client_name)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect

    def subscribe(self, topic):
        self.log.info(f"Subscribing to events from {topic}")
        self.client.subscribe(topic)

    def publish(self, topic, data):
        self.log.info(f"Publishing event with data {data} to {topic}.")
        self.client.publish(topic, data)

    def __wait_for_connection(self, exit_event, reconnect=False):
        if reconnect or self.client is None or not self.connected:
            if self.client is not None:
                self.client.disconnect()
                self.connected = False
            self.init_client()
            while not exit_event.is_set():
                try:
                    self.client.connect(
                        self.address, port=self.port, keepalive=self.keepalive
                    )
                    break
                except Exception as e:
                    self.log.error(
                        f"Cannot connect to the MQTT broker at {self.address}:{self.port}. {str(e)}. "
                        + f"Will attemmpt to reconnect after {self.reconnect_after} seconds."
                    )
                    exit_event.wait(self.reconnect_after)

    def wait_is_connected(self, exit_event, timeout=0):
        start_time = time.time()
        while (
            not exit_event.is_set()
            and not self.connected
            and (timeout == 0 or time.time() - start_time <= timeout)
        ):
            exit_event.wait(0.2)
        return self.connected

    def worker(self, exit_event):
        self.__wait_for_connection(exit_event)
        try:
            while not exit_event.is_set():
                try:
                    self.client.loop(timeout=self.loop_timeout, max_packets=1)
                    if not self.connected:
                        self.__wait_for_connection(exit_event)
                except Exception as e:
                    self.log.error(f"Error occurred in the MQTT loop. {str(e)}")
                    self.__wait_for_connection(exit_event, reconnect=True)
        finally:
            if self.connected:
                self.client.disconnect()


class Pattern:
    def __init__(self, pattern):
        self.match = None
        self.pattern = pattern
        self.re = re.compile(self.pattern)

    def __str__(self):
        return f"r'{self.pattern}'" if self.match is None else self.match.group(0)

    def __eq__(self, other):
        self.match = self.re.match(other)
        return self.match is not None


class Topic:
    def __init__(self, topic):
        self.name = topic["name"]
        self.disabled = topic.get("disabled", False)
        self.rules = []
        for rule_def in topic["rules"]:
            self.rules.append(Map(rule_def))

    def check_rule_data(self, read, data, scope, path=None):
        if path is None:
            path = []
        try:
            for k, v in read.items():
                path += [k]
                if k not in data.keys():
                    raise Exception(f"Missing property {k}.")
                else:
                    if not isinstance(v, PythonExpression) and type(v) != type(data[k]):
                        raise Exception(
                            f"Invalid type of property {'.'.join(path)}, found: {type(data[k]).__name__}, expected: {type(v).__name__}"
                        )
                    if type(v) == dict:
                        self.check_rule_data(v, data[k], scope, path)
                    else:
                        if isinstance(v, PythonExpression):
                            v = v.eval(scope)
                        if v != data[k]:
                            raise Exception(
                                f"Invalid value of property {'.'.join(path)}, found: {data[k]}, exepcted: {v}"
                            )
        except Exception as e:
            raise Exception(f"Topic data validation failed. {str(e)}")


class SerialMQTTBridge(Component):
    def __init__(self, config):
        super().__init__(config, "bridge")
        self.mqtt = None
        self.serial = None
        self.topics_serial2mqtt = []
        self.topics_mqtt2serial = []
        self._scope = None
        self.request_queue = Queue()

        ja2mqtt_file = self.config.get_dir_path(config.root("ja2mqtt"))
        ja2mqtt = Config(ja2mqtt_file, scope=self.scope(), use_template=True)
        for topic_def in ja2mqtt("serial2mqtt"):
            self.topics_serial2mqtt.append(Topic(topic_def))
        for topic_def in ja2mqtt("mqtt2serial"):
            self.topics_mqtt2serial.append(Topic(topic_def))
        self.correlation_id = ja2mqtt("options.correlation_id", None)
        self.correlation_timeout = ja2mqtt("options.correlation_timeout", 0)
        self.request = None

        self.log.info(f"The ja2mqtt definition file is {ja2mqtt_file}")
        self.log.info(
            f"There are {len(self.topics_serial2mqtt)} serial2mqtt and {len(self.topics_mqtt2serial)} mqtt2serial topics."
        )
        self.log.debug(
            f"The serial2mqtt topics are: {', '.join([x.name + ('' if not x.disabled else ' (disabled)') for x in self.topics_serial2mqtt])}"
        )
        self.log.debug(
            f"The mqtt2serial topics are: {', '.join([x.name + ('' if not x.disabled else ' (disabled)') for x in self.topics_mqtt2serial])}"
        )

    def update_correlation(self, data):
        if self.request_queue.qsize() > 0:
            self.request = self.request_queue.get()
        if self.request is not None:
            if time.time() - self.request.created_time < self.correlation_timeout and self.request.ttl > 0:
                if self.request.cor_id is not None:
                    data[self.correlation_id] = self.request.cor_id
                self.request.ttl -= 1
            else:
                self.log.debug(
                    "Discarding the request for correlation. The correlation timeout or TTL expired."
                )
                self.request = None
        return data

    def scope(self):
        if self._scope is None:
            self._scope = Map(
                topology=self.config.root("topology"),
                pattern=lambda x: Pattern(x),
                format=lambda x, **kwa: x.format(**kwa),
            )
        return self._scope

    def update_scope(self, key, value=None, remove=False):
        if self._scope is None:
            self.scope()
        if not remove:
            self._scope[key] = value
        else:
            if key in self._scope:
                del self._scope[key]

    def on_mqtt_connect(self, client, userdata, flags, rc):
        for topic in self.topics_mqtt2serial:
            self.mqtt.subscribe(topic.name)

    def on_mqtt_message(self, client, userdata, message):
        topic_name = message._topic.decode("utf-8")
        self.log.info(
            f"--> recv: {topic_name}, payload={message.payload.decode('utf-8')}"
        )

        try:
            data = Map(json.loads(str(message.payload.decode("utf-8"))))
        except Exception as e:
            raise Exception(f"Cannot parse the event data. {str(e)}")

        self.log.debug(f"The event data parsed as JSON object: {data}")
        for topic in self.topics_mqtt2serial:
            if topic.name == topic_name:
                if topic.disabled:
                    continue
                for rule in topic.rules:
                    topic.check_rule_data(rule.read, data, self.scope())
                    self.log.debug(
                        "The event data is valid according to the defined rules."
                    )
                    _data = Map(data)
                    self.update_scope("data", _data)
                    try:
                        s = deep_eval(rule.write, self._scope)
                        self.request_queue.put(
                            Map(
                                cor_id=_data.get(self.correlation_id),
                                created_time=time.time(),
                                ttl=rule.get("request_ttl",1)
                            )
                        )
                        self.serial.writeline(s)
                    finally:
                        self.update_scope("data", remove=True)

    def on_serial_data(self, data):
        if not self.mqtt.connected:
            self.log.warn(
                "No events will be published. The client is not connected to the MQTT broker."
            )
            return

        _rule = None
        current_time = time.time()
        for topic in self.topics_serial2mqtt:
            for rule in topic.rules:
                if isinstance(rule.read, PythonExpression):
                    _data = rule.read.eval(self.scope())
                else:
                    _data = rule.read
                if _data == data:
                    _rule = rule
                    if not topic.disabled:
                        self.update_scope("data", _data)
                        try:
                            d0 = self.update_correlation(Map())
                            if not rule.require_request or self.request is not None:
                                if rule.no_correlation:
                                    d0 = {}
                                d1 = deep_merge(rule.write, d0)
                                d2 = deep_eval(d1, self._scope)
                                write_data = json.dumps(d2)
                                self.log.info(
                                    f"<-- send: {topic.name}, data={write_data}"
                                )
                                self.mqtt.client.publish(topic.name, write_data)
                                break
                        finally:
                            self.update_scope("data", remove=True)
            if _rule is not None:
                break

        if _rule is None:
            self.log.debug("No rule found for the data.")

    def set_mqtt(self, mqtt):
        self.mqtt = mqtt
        self.mqtt.on_connect_ext = self.on_mqtt_connect
        self.mqtt.on_message_ext = self.on_mqtt_message

    def set_serial(self, serial):
        self.serial = serial
        serial.on_data_ext = self.on_serial_data
