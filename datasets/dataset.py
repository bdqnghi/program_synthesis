import argparse
import cPickle as pickle
import collections
import gzip
import json
import os
import random
import struct
import sys
import time

import numpy as np
import torch.utils.data

import data
import executor
import stats
from karel.mutation import KarelExampleMutator


Schema = collections.namedtuple("Schema", ["args", "return_type"])


def relpath(path):
    return os.path.join(os.path.dirname(__file__), path)


class CodeFunc(object):

    def __init__(
            self, name, schema,
            code_tree, code_sequence):
        self.name = name
        self.schema = schema
        self.code_tree = code_tree
        self.code_sequence = code_sequence

    def to_dict(self):
        return {
            'name': self.name,
            'return_type': self.schema.return_type,
            'args': self.schema.args,
            'code_tree': self.code_tree,
        }

    @classmethod
    def from_dict(cls, d):
        # TODO: Don't pass None for code_sequence
        return cls(d['name'],
                   Schema(d['args'], d['return_type']), d['short_tree'], None)


class CodeExample(object):

    def __init__(
            self, text, schema, input_tests,
            code_tree, code_sequence, funcs, tests,
            candidate_code_sequence=None,
            task_types=[], tags=[], language='lisp'):
        self.text = text
        self.schema = schema
        self.input_tests = input_tests
        self.code_tree = code_tree
        self.code_sequence = code_sequence
        self.funcs = funcs
        self.tests = tests
        # Add candidate_code_tree in the future
        self.candidate_code_sequence = candidate_code_sequence
        self.task_types = task_types
        self.tags = tags
        self.language = language

    def to_dict(self):
        return {
            'text': self.text,
            'return_type': self.schema.return_type,
            'args': self.schema.args,
            'code_sequence': self.code_sequence,
            'code_tree': self.code_tree,
            'funcs': [f.to_dict() for f in self.funcs],
            'tests': self.input_tests + self.tests,
            'tags': self.tags,
            'nodes': self.task_types,
            'language': self.language
        }

    @classmethod
    def from_dict(cls, d, input_test_ratio=0.7):
        input_test_count = int(len(d['tests']) * input_test_ratio)
        return cls(
            d['text'],
            Schema(d['args'], d['return_type']),
            d['tests'][:input_test_count],
            d['short_tree'],
            d['code_sequence'], [CodeFunc.from_dict(f) for f in d['funcs']],
            d['tests'][input_test_count:],
            task_types=d['nodes'],
            tags=d['tags'])


class KarelExample(object):
    __slots__ = (
        'guid',
        'code_sequence',
        'input_tests',
        'tests',
        'text',
        'ref_example', )
    schema = Schema(None, None)
    code_tree = []
    _empty_trace = executor.KarelTrace([], [])

    def __init__(self, guid, code_sequence, input_tests, tests,
            ref_example=None):
        self.guid = guid
        self.code_sequence = code_sequence
        self.input_tests = input_tests
        self.tests = tests
        self.text = code_sequence
        self.ref_example = ref_example

    @classmethod
    def from_dict(cls, d):
        all_examples = []
        for example in d['examples']:
            ex = {
                'input': sorted(list(int(x) for x in example['in'])),
                'output': sorted(list(int(x) for x in example['out']))
            }
            if 'trace_grids' in example:
                ex['trace'] = executor.KarelTrace(
                        grids=example['trace_grids'],
                        events=[])
            all_examples.append(ex)
        assert len(all_examples) == 6
        ref_dict = d.get('ref')
        if ref_dict:
            ref_example = KarelExample.from_dict(ref_dict)
        else:
            ref_example = None
        return cls(d['guid'], d['code'], all_examples[:5], all_examples,
                ref_example)

    def to_dict(self):
        return {
            'guid': self.guid,
            'examples': [{
                'in': example['input'],
                'out': example['output'],
                'trace_grids': example.get('trace', self._empty_trace).grids,
            } for example in self.tests],
            'code': self.code_sequence,
            'ref': self.ref_example.to_dict() if self.ref_example else None
        }


class BucketizedSampler(object):

    def __init__(self, dataset, buckets, bucket_key, adaptive_size=None):
        self.dataset = dataset
        self.buckets = buckets
        self.adaptive_size = adaptive_size
        self.bucket_ids = {k: [] for k in self.buckets}
        for idx, example in enumerate(self.dataset.data):
            key = bucket_key(example)
            self.bucket_ids[key].append(idx)
        print("Buckets: " + ", ".join(['%s: %s' % (key, len(self.bucket_ids[key])) for key in buckets]))

    def __len__(self):
        return len(self.dataset)

    def __iter__(self):
        if self.dataset.shuffle:
            for key in self.bucket_ids:
                random.shuffle(self.bucket_ids[key])
        self._last_i = {key: 0 for key in self.bucket_ids}
        return self

    def next(self):
        non_empty_keys = [key for key in self.bucket_ids if self._last_i[key] < len(self.bucket_ids[key])]
        if not non_empty_keys:
            raise StopIteration
        res = []
        key = random.choice(non_empty_keys)
        while self._last_i[key] < len(self.bucket_ids[key]) and len(res) < self.dataset.batch_size:
            res.append(self.dataset.data[self.bucket_ids[key][self._last_i[key]]])
            self._last_i[key] += 1
            if self.adaptive_size and self.adaptive_size(res):
                break
        return res


class Dataset(object):

    def __init__(self, batch_size, data, shuffle=False):
        self.batch_size = batch_size
        self.data = data
        self.shuffle = shuffle

    def __iter__(self):
        self._index = range(len(self.data))
        if self.shuffle:
            random.shuffle(self._index)
        self._last_i = 0
        return self

    def __len__(self):
        return (len(self.data) - 1) // self.batch_size + 1

    def next(self):
        if self._last_i == len(self.data):
            raise StopIteration
        res = []
        while self._last_i < len(self.data) and len(res) < self.batch_size:
            res.append(self.data[self._index[self._last_i]])
            self._last_i += 1
        return res

    def build_vocab(self, min_freq=50):
        freqs = collections.defaultdict(int)
        def update_freqs(words):
            for word in words:
                freqs[word] += 1
        for example in self.data:
            update_freqs(example.text)
            update_freqs(example.code_sequence)
            for column in example.schema.args.iteritems():
                update_freqs(column)
        return data.get_vocab(freqs, min_freq)

    def save(self, filename):
        with open(filename, 'w') as f:
            for example in self.data:
                f.write(json.dumps(example.to_dict()) + "\n")


class DynamicDataset(object):
    SHARD_SIZE = 100

    def __init__(self, batch_size, capacity=None, min_items=None, path=None):
        self.items = collections.deque([], maxlen=capacity)
        self.batch_size = batch_size
        self.capacity = capacity
        self.min_items = min_items
        if self.min_items and self.capacity:
            assert self.capacity >= self.min_items

        self.path = path
        if self.path is not None:
            self.shard_sizes = collections.deque()
            self.shard_items_count = 0
            if os.path.exists(self.path):
                entries =  os.listdir(self.path)
                entries.sort(key=int)
                print('Loading from {}...'.format(self.path))
                for entry in entries:
                    with gzip.open(os.path.join(self.path, entry)) as f:
                        shard = pickle.load(f)
                        self.shard_items_count += len(shard)
                        self.shard_sizes.append(len(shard))
                        self.items.extend(shard)
                print('Done.')

                if entries:
                    self.earliest_shard = int(entries[0])
                    self.next_shard = int(entries[-1]) + 1
                else:
                    self.earliest_shard = 0
                    self.next_shard = 0
                self.candidate_shard = []
            else:
                os.mkdir(self.path)
                self.earliest_shard = 0
                self.next_shard =  0
                self.candidate_shard = []

    def next(self):
        if len(self.items) <= self.batch_size:
            return list(self.items)

        return random.sample(self.items, self.batch_size)

    def add(self, item):
        self.items.append(item)
        if self.path is None:
            return

        self.candidate_shard.append(item)
        if len(self.candidate_shard) == DynamicDataset.SHARD_SIZE:
            with gzip.open(os.path.join(self.path, str(self.next_shard)), 'w') as f:
                pickle.dump(self.candidate_shard, f, pickle.HIGHEST_PROTOCOL)
            self.shard_items_count += DynamicDataset.SHARD_SIZE
            self.shard_sizes.append(DynamicDataset.SHARD_SIZE)
            self.next_shard += 1
            self.candidate_shard = []

            while self.shard_items_count - self.shard_sizes[0] >= self.capacity:
                self.shard_sizes.popleft()
                os.unlink(os.path.join(self.path, str(self.earliest_shard)))
                self.earliest_shard += 1

    def __len__(self):
        return (len(self.items) - 1) // self.batch_size + 1

    def is_ready(self):
        if self.min_items:
            return len(self.items) > self.min_items
        return bool(self.items)


class NearDataset(Dataset):

    def __init__(
            self, filename, batch_size, shuffle=False, max_size=0, max_code_length=0,
            filter_code_length=0):
        tasks = []
        with open(filename) as f:
            for line in f:
                try:
                    line = json.loads(line)
                except ValueError:
                    continue
                args = line['args']
                if not isinstance(args, dict):
                    args = collections.OrderedDict(args)
                return_type = line.get('return_type', None)
                language = line['language'] if 'language' in line else 'lisp'
                if 'text' in line:
                    text = line['text']
                    if not isinstance(text, list):
                        try:
                            text = data.tokenize_text_line(text)
                        except Exception as e:
                            print("Exception while tokenizing %s" % text)
                            print(e)
                            continue
                else:
                    try:
                        text = data.tokenize_text_line(line['statement'])
                    except Exception as e:
                        print("Exception while tokenizing %s" % line['statement'])
                        print(e)
                        continue
                funcs = [
                    CodeFunc(
                        name=func['name'],
                        schema=Schema(func['args'], func['return_type']),
                        code_tree=func['short_tree'],
                        code_sequence=data.flatten_code(func['short_tree']))
                    for func in line['funcs']
                ] if 'funcs' in line else []

                code_tree = code_sequence = None
                if 'short_tree' in line and line['short_tree']:
                    code_tree = line['short_tree']
                    code_sequence = data.flatten_code(code_tree, language)
                elif 'code_tree' in line and line['code_tree']:
                    code_tree = line['code_tree']
                    if 'code_sequence' in line and line['code_sequence']:
                        code_sequence = line['code_sequence']
                    else:
                        code_sequence = data.flatten_code(code_tree, language)
                elif 'code_sequence' in line:
                    code_sequence = line['code_sequence']
                if not isinstance(code_sequence, list):
                    code_sequence = data.tokenize_code_line(line['code_sequence'])
                if filter_code_length > 0 and len(code_sequence) > filter_code_length:
                    continue
                if max_code_length > 0 and code_sequence is not None:
                    code_sequence = code_sequence[:max_code_length]

                if not code_tree and not code_sequence:
                    print("Found no code in record: %s" % line)
                    continue

                tasks.append(CodeExample(
                    text=text,
                    schema=Schema(args, return_type),
                    code_sequence=code_sequence,
                    code_tree=code_tree,
                    funcs=funcs,
                    input_tests=line['tests'][:3],
                    tests=line['tests'][3:],
                    task_types=line['nodes'] if 'nodes' in line else [],
                    tags=line['tags'] if 'tags' in line else [],
                    language=language
                ))
                if max_size > 0 and len(tasks) >= max_size:
                    break
        super(NearDataset, self).__init__(batch_size, tasks, shuffle)


class KarelTorchDataset(torch.utils.data.Dataset):

    def __init__(self, filename, mutator=lambda x: x):
        self.filename = filename
        self.mutator = mutator

        self.file = None
        self.index = []
        with open(self.filename + '.index') as index_file:
            while True:
                offset = index_file.read(8)
                if not offset:
                    break
                offset, = struct.unpack('<Q', offset)
                self.index.append(offset)

    def __len__(self):
        return len(self.index)

    def __getitem__(self, idx):
        if self.file is None:
            self.file = open(self.filename)
        self.file.seek(self.index[idx])
        return self.mutator(KarelExample.from_dict(pickle.load(self.file)))


class KarelDataset(object):

    def __init__(self, filename, batch_size, mutator=lambda x: x):
        self.filename = filename
        self.batch_size = batch_size
        self.file = open(self.filename)
        self.mutator = mutator

    def __iter__(self):
        self.file.seek(0)
        return self

    def next(self):
        res = []
        try:
            while len(res) < self.batch_size:
                res.append(
                    self.mutator(
                        KarelExample.from_dict(pickle.load(self.file))))
        except EOFError:
            pass
        if not res:
            raise StopIteration
        return res

    def build_vocab(self):
        tokens = collections.defaultdict(int)
        self.file.seek(0)
        while True:
            try:
                example = pickle.load(self.file)
            except EOFError:
                break
            for token in example['code']:
                tokens[token] += 1
        return data.get_vocab(tokens, 1)


def get_algolisp_dataset(args):
    args.word_vocab = relpath('../data/algolisp/word.vocab')
    train_data = NearDataset(
        relpath('../data/algolisp/dataset.train.jsonl'),
        args.batch_size, shuffle=True, max_size=args.dataset_max_size,
        max_code_length=args.dataset_max_code_length)
    if not os.path.exists(args.word_vocab):
        data.save_vocab(args.word_vocab, train_data.build_vocab(min_freq=args.vocab_min_freq))
    dev_data = NearDataset(
        relpath('../data/algolisp/dataset.dev.jsonl'),
        args.batch_size, shuffle=False)
    return train_data, dev_data


def get_karel_dataset(args):
    suffix = args.dataset[5:]
    args.word_vocab = relpath('../data/karel/word.vocab')

    if args.karel_mutate_ref:
        mutation_dist = [float(x) for x in args.karel_mutate_n_dist.split(',')]
        train_mutator = KarelExampleMutator(mutation_dist, rng_fixed=False,
                add_trace=False)
        dev_mutator = KarelExampleMutator(mutation_dist, rng_fixed=True,
                add_trace=False)
    else:
        train_mutator = dev_mutator = lambda x: x

    train_data = torch.utils.data.DataLoader(
        KarelTorchDataset(
            relpath('../data/karel/train{}.pkl'.format(suffix)),
            train_mutator),
        args.batch_size,
        collate_fn=lambda x: x,
        num_workers=4)
    if not os.path.exists(args.word_vocab):
        data.save_vocab(args.word_vocab, train_data.build_vocab())
    dev_data = torch.utils.data.DataLoader(
        KarelTorchDataset(
            relpath('../data/karel/val{}.pkl'.format(suffix)),
            dev_mutator),
        args.batch_size,
        collate_fn=lambda x: x,
        num_workers=2)
    return train_data, dev_data


def get_algolisp_eval_dataset(args):
    args.word_vocab = relpath('../data/algolisp/word.vocab')
    return NearDataset(
        relpath('../data/algolisp/dataset.dev.jsonl'),
        args.batch_size, shuffle=True, max_size=args.dataset_max_size)


def get_karel_eval_dataset(args):
    suffix = args.dataset[5:]
    args.word_vocab = relpath('../data/karel/word.vocab')

    if args.karel_mutate_ref:
        mutation_dist = [float(x) for x in args.karel_mutate_n_dist.split(',')]
        dev_mutator = KarelExampleMutator(mutation_dist, rng_fixed=True,
                add_trace=False)
    else:
        dev_mutator = lambda x: x

    dev_data = torch.utils.data.DataLoader(
        KarelTorchDataset(
            relpath('../data/karel/val{}.pkl'.format(suffix)),
            dev_mutator),
        args.batch_size,
        collate_fn=lambda x: x,
        num_workers=2)
    return dev_data


def get_dataset(args):
    if args.dataset == 'algolisp':
        return get_algolisp_dataset(args)
    elif args.dataset.startswith('karel'):
        return get_karel_dataset(args)
    else:
        raise ValueError("Unknown dataset %s" % args.dataset)


def get_eval_dataset(args):
    if args.dataset == 'algolisp':
        return get_algolisp_eval_dataset(args)
    elif args.dataset.startswith('karel'):
        return get_karel_eval_dataset(args)
    else:
        raise ValueError("Unknown dataset %s" % args.dataset)


def dataset_split(args, dataset, filenames, proportions):
    def _renormalize(lst):
        total = sum(lst)
        return [float(x) / total for x in lst]
    datastats = [stats.DatasetStats(args) for _ in filenames]
    files = [open(filename, 'w') for filename in filenames]
    real_proportions = [x for x in proportions]
    candidates = range(len(proportions))
    expected_size = [len(dataset.data) * p for p in proportions]
    for example in dataset.data:
        fidx = -1
        for i, s in enumerate(datastats):
            if str(example.code_sequence) in s.code_map or str(example.text) in s.text_map:
                fidx = i
        if fidx == -1:
            fidx = np.random.choice(candidates, p=proportions)
        datastats[fidx].update(example)
        files[fidx].write(json.dumps(example.to_dict()) + "\n")
        if datastats[fidx].stats['total'] >= expected_size[fidx] and fidx in candidates:
            idx = candidates.index(fidx)
            candidates.pop(idx)
            proportions.pop(idx)
            proportions = _renormalize(proportions)

    for f in files:
        f.close()
    for i, ds in enumerate(datastats):
        print("=== %.2f%% ===" % real_proportions[i])
        ds.display()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--train-test-split', action='store_true', default=False)
    parser.add_argument('--train-dev-split', action='store_true', default=False)
    parser.add_argument('--original', type=str, default=None)
    parser.add_argument('--show_tags', action='store_true', default=False)
    parsed_args, _ = parser.parse_known_args(sys.argv)

    if parsed_args.train_test_split:
        d = NearDataset(parsed_args.original, batch_size=1, shuffle=False)
        print("Loaded dataset from %s" % parsed_args.original)
        dataset_split(
            parsed_args, d,
            ["../data/algolisp/dataset.train.jsonl",
            "../data/algolisp/dataset.dev.jsonl",
            "../data/algolisp/dataset.test.jsonl"],
            [0.8, 0.1, 0.1])
