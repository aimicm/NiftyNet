# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function

import tensorflow as tf
from tensorflow.contrib.framework import list_variables

from niftynet.io.misc_io import image3_axial
from niftynet.io.misc_io import image3_coronal
from niftynet.io.misc_io import image3_sagittal
from niftynet.io.misc_io import resolve_checkpoint
from niftynet.utilities import util_common as util
from niftynet.utilities.restore_initializer import restore_initializer
from niftynet.utilities.util_common import look_up_operations

RESTORABLE = 'NiftyNetObjectsToRestore'
NETORK_OUTPUT = 'niftynetout'
CONSOLE = 'niftynetconsole'
TF_SUMMARIES = tf.GraphKeys.SUMMARIES
SUPPORTED_SUMMARY = {'scalar': tf.summary.scalar,
                     'histogram': tf.summary.histogram,
                     'image3_sagittal': image3_sagittal,
                     'image3_coronal': image3_coronal,
                     'image3_axial': image3_axial}


class GradientsCollector(object):
    def __init__(self, n_devices=1):
        self._gradients = []
        self.n_devices = n_devices

    def add_to_collection(self, gradients):
        self._gradients.append(gradients)
        assert self.current_tower_id <= self.n_devices, \
            "call add_to_collection once per device"

    @property
    def current_tower_id(self):
        return len(self._gradients)

    @property
    def gradients(self):
        # return averaged over devices
        assert self._gradients, \
            "Please add gradients to collector when constructing the graph"
        return util.average_gradients(self._gradients)


class OutputsCollector(object):
    def __init__(self, n_devices=1):
        self.console_vars = {}
        self.summary_vars = {}
        self.output_vars = {}

        self._merge_op = None
        self.n_devices = n_devices

    def _add_to_dict(self, var_dict, var, name, do_averaging):
        """
        update the dict, with item of either
        {name: variable} or {name: list of variable}
        """
        assert isinstance(var, tf.Tensor), \
            "only supports adding one tf.Tensor at a time," \
            "but received {}".format(var)

        if do_averaging and self.n_devices > 1:
            # collecting variables across devices as a list
            var_list = var_dict.get(name, [])
            assert isinstance(var_list, list), \
                "averaged variable name {} has been taken".format(name)
            var_list.append(var)
            var_dict[name] = var_list
            assert len(var_list) <= self.n_devices, \
                "averaged variable {} has been used " \
                "in the collector".format(name)
        else:
            # collecting variables and rename if exists
            new_name = name
            _uniq_id = 0
            while new_name in var_dict:
                _uniq_id += 1
                new_name = '{}_{}'.format(name, _uniq_id)
            var_dict[new_name] = var

    def add_to_collection(self, var, name,
                          average_over_devices=False,
                          collection=CONSOLE,
                          summary_type=None):
        if collection == CONSOLE:
            self._add_to_console(var, name, average_over_devices)
        elif collection == NETORK_OUTPUT:
            self._add_to_network_output(var, name, average_over_devices)
        elif collection == TF_SUMMARIES:
            self._add_to_tf_summary(
                var, name, average_over_devices, summary_type)
        else:
            raise ValueError(
                "unknown variable collection {}.".format(collection))

    def variables(self, collection=CONSOLE):
        if collection == CONSOLE:
            return self.console_vars
        elif collection == TF_SUMMARIES:
            return self._merge_op if self._merge_op is not None else {}
        elif collection == NETORK_OUTPUT:
            return self.output_vars
        else:
            raise ValueError("unknown output variables type_str")

    def finalise_output_op(self):
        """
        This function checks the dictionary, if the variable needs to
        be averaged over devices, then a reduce_mean node is added to
        the graph.
        This function should be called in
        ApplicationDriver.create_graph function
        """
        self._average_variables_over_devices(self.console_vars, False)
        self._average_variables_over_devices(self.output_vars, False)
        self._average_variables_over_devices(self.summary_vars, True)
        self._merge_op = tf.summary.merge_all(key=TF_SUMMARIES)

    def _add_to_network_output(self, var, name, average_over_devices=False):
        self._add_to_dict(self.output_vars, var, name, average_over_devices)

    def _add_to_console(self, var, name, average_over_devices=False):
        self._add_to_dict(self.console_vars, var, name, average_over_devices)

    def _add_to_tf_summary(self, var, name,
                           average_over_devices=False, summary_type='scalar'):
        self._add_to_dict(self.summary_vars, var, name, average_over_devices)
        values = self.summary_vars.get(name, None)
        if isinstance(values, tf.Tensor):
            summary_op = look_up_operations(summary_type, SUPPORTED_SUMMARY)
            summary_op(name=name, tensor=values, collections=[TF_SUMMARIES])

    @staticmethod
    def _average_variables_over_devices(var_dict, create_tf_summary_op=False):
        for var_name in var_dict:
            values = var_dict.get(var_name, None)
            if not isinstance(values, list):
                continue
            var_dict[var_name] = tf.reduce_mean(values, name=var_name)
            if create_tf_summary_op:
                tf.summary.scalar(name='{}_device_average_'.format(var_name),
                                  tensor=var_dict[var_name],
                                  collections=[TF_SUMMARIES])


def global_variables_initialize_or_restorer(var_list=None):
    # For any scope added to RESTORABLE collection:
    # variable will be restored from a checkpoint if it exists in the
    # specified checkpoint and no scope ancestor can restore it.
    if var_list is None:
        var_list = tf.global_variables()
    restorable = sorted(tf.get_collection(RESTORABLE), key=lambda x: x[0])
    restored_vars = {}
    for scope, checkpoint_name, checkpoint_scope in restorable:
        variables_in_scope = tf.get_collection(
            tf.GraphKeys.GLOBAL_VARIABLES, scope=scope)
        checkpoint_file = resolve_checkpoint(checkpoint_name)
        variables_in_file = [v for (v, _) in list_variables(checkpoint_file)]
        rename = lambda x: x.replace(scope, checkpoint_scope).replace(':0', '')
        to_restore = [v for v in variables_in_scope
                      if v in var_list and rename(v.name) in variables_in_file]
        for var in to_restore:
            if var in restored_vars:
                continue
            if '/' in rename(var.name):
                checkpoint_subscope, var_name = rename(var.name).rsplit('/', 1)
            else:
                checkpoint_subscope, var_name = None, rename(var.name)
            initializer = restore_initializer(
                checkpoint_name, var_name, checkpoint_subscope)
            restored_vars[var] = tf.assign(
                var, initializer(var.get_shape(), dtype=var.dtype))
    init_others = tf.variables_initializer(
        [v for v in var_list if v not in restored_vars])
    restore_op = tf.group(init_others, *list(restored_vars.values()))
    return restore_op
