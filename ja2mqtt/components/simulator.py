# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas@vitvar.com

from __future__ import absolute_import, unicode_literals

import json
import logging
import re
import threading
import time
import random
from queue import Empty, Queue

import paho.mqtt.client as mqtt
import serial as py_serial

from ja2mqtt.config import Config
from ja2mqtt.utils import Map, PythonExpression, deep_eval, deep_merge, merge_dicts

ERROR_INVALID_VALUE = "ERROR: 4 INVALID_VALUE"
ERROR_NO_ACCESS = "ERROR: 3 NO_ACCESS"


class SimilatorException(Exception):
    pass


class Section:
    def __init__(self, data):
        self.code = data.code
        self.state = data.state

    def __str__(self):
        return f"STATE {self.code} {self.state}"

    def __repr__(self):
        return self.__str__()

    def set(self):
        if self.state == "ARMED":
            return ERROR_INVALID_VALUE
        if self.state == "READY":
            self.state = "ARMED"
            return self.__str__()
        raise SimilatorException(f"Cannot run command SET. Invalid state {self.state}.")

    def unset(self):
        if self.state == "READY":
            return ERROR_INVALID_VALUE
        if self.state == "ARMED":
            self.state = "READY"
            return self.__str__()
        raise SimilatorException(
            f"Cannot run command UNSET. Invalid state {self.state}."
        )


class Simulator:
    def __init__(self, config, encoding):
        self.log = logging.getLogger("simulator")
        self.config = config
        self.response_delay = config.value_int("response_delay", default=0.5)
        self.prf_state_bits = config.value_int("prf_state_bits", default=24)
        self.rules = [Map(x) for x in config.value("rules")]
        self.sections = {
            str(x["code"]): Section(Map(x)) for x in config.value("sections")
        }
        self.pin = config.value("pin")
        self.timeout = 1
        self.buffer = Queue()
        self.encoding = encoding

    def __str__(self):
        return (
            f"{self.__class__}: pin={self.pin}, timeout={self.timeout}, response_delay={self.response_delay}, "
            + f"prf_state_bits={self.prf_state_bits}, sections={[str(x) for x in self.sections.values()]}, rules={self.rules}"
        )

    def open(self, exit_event):
        pass

    def close(self):
        pass

    def _add_to_buffer(self, data):
        time.sleep(self.response_delay)
        self.buffer.put(data)

    def write(self, data):
        def _match(pattern, data_str):
            m = re.compile(pattern).match(data_str)
            if m:
                return Map(m.groupdict())
            else:
                return None

        def _check_pin(command):
            if command.pin != str(self.pin):
                self._add_to_buffer(ERROR_NO_ACCESS)
                return False
            return True

        data_str = data.decode(self.encoding).strip("\n")

        # SET and UNSET commands
        command = _match(
            "^(?P<pin>[0-9]+) (?P<command>SET|UNSET) (?P<code>[0-9]+)$",
            data_str,
        )
        if command is not None and _check_pin(command):
            section = self.sections.get(command.code)
            if section is not None:
                data = {
                    "SET": lambda _: section.set(),
                    "UNSET": lambda _: section.unset(),
                    "N/A": lambda x: (_ for _ in ()).throw(
                        SimilatorException(f"The command {x} is not implemented.")
                    ),
                }.get(command.command, "N/A")(command.command)
                self._add_to_buffer(data)
            else:
                self._add_to_buffer(ERROR_INVALID_VALUE)
            return

        # STATE command
        command = _match(
            "^(?P<pin>[0-9]+) (?P<command>STATE)( (?P<code>[0-9]+))?$", data_str
        )
        if command is not None and _check_pin(command):
            print(command)
            sections = [
                x
                for x in self.sections.values()
                if command.code is None or str(x.code) == str(command.code)
            ]
            time.sleep(self.response_delay)
            for section in sections:
                self.buffer.put(str(section))
            return

    def readline(self):
        try:
            return bytes(self.buffer.get(timeout=self.timeout), self.encoding)
        except Empty:
            return b""

    def scope(self):

        from .serial import encode_prfstate

        def _prf_random_states(*pos, on_prob=0.5):
            prf = { str(p):("ON" if random.random() < on_prob else "OFF") for p in pos }
            return "PRFSTATE " + encode_prfstate(prf, self.prf_state_bits)

        return Map(
            random = lambda a,b: a+round(random.random()*b),
            prf_randon_states = _prf_random_states,
        )

    def worker(self, exit_event):

        _scope = self.scope()

        def _value(v):
            if isinstance(v, PythonExpression):
                return v.eval(_scope)
            else:
                return v

        try:
            while not exit_event.is_set():
                current_time = time.time()
                for rule in self.rules:
                    if rule.get("time_next"):
                        if rule.__last_write is None:
                            rule.__last_write = current_time
                        if current_time - rule.__last_write > _value(rule.time_next):
                            self.buffer.put(_value(rule.write))
                            rule.__last_write = current_time
                exit_event.wait(0.5)
        finally:
            self.log.info("Simulator worker ended.")

    def start(self, exit_event):
        self.thread = threading.Thread(
            target=self.worker, args=(exit_event,), daemon=True
        )
        self.thread.start()

    def join(self):
        if self.thread is not None and self.thread.is_alive():
            self.thread.join()
