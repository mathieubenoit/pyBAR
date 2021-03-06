import logging
from sys import maxint
import glob
import zmq
from threading import RLock
import tables as tb
import os.path
from os import remove

from pybar.analysis.RawDataConverter.data_struct import MetaTableV2 as MetaTable, generate_scan_parameter_description


def send_data(socket, data, scan_parameters, name='FEI4readoutData', flags=0, copy=False, track=False):
    if socket and data:  # if socket is defined send the data
        try:
            data_meta_data = dict(
                name=name,
                dtype=str(data[0].dtype),
                shape=data[0].shape,
                timestamp_start=data[1],
                timestamp_stop=data[2],
                readout_error=float(data[3]),
                scan_parameters=str(scan_parameters)
            )
            socket.send_json(data_meta_data, flags | zmq.SNDMORE | zmq.NOBLOCK)
            return socket.send(data[0].tostring(), flags, copy=copy, track=track)
        except zmq.ZMQError:
            pass


def open_raw_data_file(filename, mode="w", title="", scan_parameters=None, socket_addr=None, **kwargs):
    '''Mimics pytables.open_file()

    Returns:
    RawDataFile Object

    Examples:
    with open_raw_data_file(filename = self.scan_data_filename, title=self.scan_id, scan_parameters=[scan_parameter]) as raw_data_file:
        # do something here
        raw_data_file.append(self.readout.data, scan_parameters={scan_parameter:scan_parameter_value})
    '''
    return RawDataFile(filename=filename, mode=mode, title=title, scan_parameters=scan_parameters, socket_addr=socket_addr, **kwargs)


class RawDataFile(object):
    '''Raw data file object. Saving data queue to HDF5 file.
    '''
    def __init__(self, filename, mode="w", title='', scan_parameters=None, socket_addr=None, **kwargs):  # mode="r+" to append data, raw_data_file_h5 must exist, "w" to overwrite raw_data_file_h5, "a" to append data, if raw_data_file_h5 does not exist it is created):
        self.lock = RLock()
        if os.path.splitext(filename)[1].strip().lower() != '.h5':
            self.base_filename = filename
        else:
            self.base_filename = os.path.splitext(filename)[0]
        if isinstance(scan_parameters, dict):
            self.scan_parameters = scan_parameters
        elif isinstance(scan_parameters, (list, tuple)):
            self.scan_parameters = dict.fromkeys(scan_parameters)
        else:
            self.scan_parameters = {}
        self.raw_data_earray = None
        self.meta_data_table = None
        self.scan_param_table = None
        self.h5_file = None
        if mode and mode[0] == 'w':
            h5_files = glob.glob(os.path.splitext(filename)[0] + '*.h5')
            if h5_files:
                logging.info('Removing following file(s): %s' % ', '.join(h5_files))
            for h5_file in h5_files:
                remove(h5_file)
        # list of filenames and index
        self.curr_filename = self.base_filename
        self.filenames = {self.curr_filename: 0}
        self.open(self.curr_filename, mode, title, **kwargs)
        if socket_addr:
            self.socket = zmq.Context().socket(zmq.PUSH)
            self.socket.bind(socket_addr)
        else:
            self.socket = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False  # do not hide exceptions

    def open(self, filename, mode='w', title='', **kwargs):
        if os.path.splitext(filename)[1].strip().lower() != '.h5':
            filename = os.path.splitext(filename)[0] + '.h5'
        if os.path.isfile(filename) and mode in ('r+', 'a'):
            logging.info('Opening existing raw data file: %s' % filename)
        else:
            logging.info('Opening new raw data file: %s' % filename)

        filter_raw_data = tb.Filters(complib='blosc', complevel=5, fletcher32=False)
        filter_tables = tb.Filters(complib='zlib', complevel=5, fletcher32=False)
        self.h5_file = tb.open_file(filename, mode=mode, title=title if title else filename, **kwargs)
        try:
            self.raw_data_earray = self.h5_file.createEArray(self.h5_file.root, name='raw_data', atom=tb.UIntAtom(), shape=(0,), title='raw_data', filters=filter_raw_data)  # expectedrows = ???
        except tb.exceptions.NodeError:
            self.raw_data_earray = self.h5_file.getNode(self.h5_file.root, name='raw_data')
        try:
            self.meta_data_table = self.h5_file.createTable(self.h5_file.root, name='meta_data', description=MetaTable, title='meta_data', filters=filter_tables)
        except tb.exceptions.NodeError:
            self.meta_data_table = self.h5_file.getNode(self.h5_file.root, name='meta_data')
        if self.scan_parameters:
            try:
                scan_param_descr = generate_scan_parameter_description(self.scan_parameters)
                self.scan_param_table = self.h5_file.createTable(self.h5_file.root, name='scan_parameters', description=scan_param_descr, title='scan_parameters', filters=filter_tables)
            except tb.exceptions.NodeError:
                self.scan_param_table = self.h5_file.getNode(self.h5_file.root, name='scan_parameters')

    def close(self):
        with self.lock:
            self.flush()
            logging.info('Closing raw data file: %s' % self.h5_file.filename)
            self.h5_file.close()

    def append_item(self, data_tuple, scan_parameters=None, new_file=False, flush=True):
        with self.lock:
            if scan_parameters:
                # check for not existing keys
                diff = set(scan_parameters).difference(set(self.scan_parameters))
                if diff:
                    raise ValueError('Unknown scan parameter(s): %s' % ', '.join(diff))
                # parameters that have changed
                diff = [name for name in scan_parameters.keys() if scan_parameters[name] != self.scan_parameters[name]]
                self.scan_parameters.update(scan_parameters)
                if (new_file is True and diff) or (isinstance(new_file, (list, tuple)) and len([name for name in diff if name in new_file]) != 0):
                    self.curr_filename = os.path.splitext(self.base_filename)[0].strip().lower() + '_' + '_'.join([str(item) for item in reduce(lambda x, y: x + y, [(key, value) for key, value in scan_parameters.items() if (new_file is True or (isinstance(new_file, (list, tuple)) and key in new_file))])])
                    index = self.filenames.get(self.curr_filename, 0)
                    if index == 0:
                        filename = self.curr_filename + '.h5'
                        self.filenames[self.curr_filename] = 0  # add to dict
                    else:
                        filename = self.curr_filename + '_' + str(index) + '.h5'
                    # copy nodes to new file
                    nodes = self.h5_file.list_nodes('/', classname='Group')
                    with tb.open_file(filename, mode='a', title=filename) as h5_file:  # append, since file can already exists when scan parameters are jumping back and forth
                        for node in nodes:
                            self.h5_file.copy_node(node, h5_file.root, overwrite=True, recursive=True)
                    self.close()
                    self.open(filename, 'a', filename)
            total_words = self.raw_data_earray.nrows
            raw_data = data_tuple[0]
            len_raw_data = raw_data.shape[0]
            if total_words + len_raw_data > maxint:
                    index = self.filenames.get(self.curr_filename, 0) + 1  # reached file size limit, increase index by one
                    self.filenames[self.curr_filename] = index  # update dict
                    filename = self.curr_filename + '_' + str(index) + '.h5'
                    # copy nodes to new file
                    nodes = self.h5_file.list_nodes('/', classname='Group')
                    with tb.open_file(filename, mode='a', title=filename) as h5_file:  # append, since file can already exists when scan parameters are jumping back and forth
                        for node in nodes:
                            self.h5_file.copy_node(node, h5_file.root, overwrite=True, recursive=True)
                    self.close()
                    self.open(filename, 'a', filename)
                    total_words = self.raw_data_earray.nrows  # in case of re-opening existing file
            self.raw_data_earray.append(raw_data)
            self.meta_data_table.row['timestamp_start'] = data_tuple[1]
            self.meta_data_table.row['timestamp_stop'] = data_tuple[2]
            self.meta_data_table.row['error'] = data_tuple[3]
            self.meta_data_table.row['data_length'] = len_raw_data
            self.meta_data_table.row['index_start'] = total_words
            total_words += len_raw_data
            self.meta_data_table.row['index_stop'] = total_words
            self.meta_data_table.row.append()
            if self.scan_parameters:
                for key in self.scan_parameters:
                    self.scan_param_table.row[key] = self.scan_parameters[key]
                self.scan_param_table.row.append()
            if flush:
                self.flush()
            send_data(self.socket, data_tuple, self.scan_parameters)

    def append(self, data_iterable, scan_parameters=None, flush=True):
        with self.lock:
            for data_tuple in data_iterable:
                self.append_item(data_tuple, scan_parameters, flush=False)
            if flush:
                self.flush()

    def flush(self):
        with self.lock:
            self.raw_data_earray.flush()
            self.meta_data_table.flush()
            if self.scan_parameters:
                self.scan_param_table.flush()


def save_raw_data_from_data_queue(data_queue, filename, mode='a', title='', scan_parameters={}, **kwargs):  # mode="r+" to append data, raw_data_file_h5 must exist, "w" to overwrite raw_data_file_h5, "a" to append data, if raw_data_file_h5 does not exist it is created
    '''Writing raw data file from data queue

    If you need to write raw data once in a while this function may make it easy for you.
    '''
    with open_raw_data_file(filename, mode='a', title='', scan_parameters=list(dict.iterkeys(scan_parameters)), **kwargs) as raw_data_file:
        raw_data_file.append(data_queue, scan_parameters=scan_parameters, **kwargs)
