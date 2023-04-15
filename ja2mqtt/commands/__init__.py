# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas@vitvar.com

from __future__ import absolute_import, unicode_literals

import click

import ja2mqtt.config as ja2mqtt_config
from ja2mqtt import get_version_string

from .config import command_config
from .run import command_run
from .test import command_publish


@click.group()
@click.option(
    "--no-ansi",
    "no_ansi",
    is_flag=True,
    default=False,
    help="Do not use ANSI colors",
)
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Print debug information",
)
@click.version_option(version=get_version_string())
def ja2mqtt(no_ansi, debug):
    if no_ansi:
        ja2mqtt_config.ANSI_COLORS = False
    if debug:
        ja2mqtt_config.DEBUG = True


ja2mqtt.add_command(command_run)
ja2mqtt.add_command(command_config)
ja2mqtt.add_command(command_publish)
