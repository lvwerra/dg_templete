from datasets import Dataset
import general_files.utils.common_util as utils
from general_files.utils.data_util import print_dataset_overview

log = utils.get_logger(__name__)


class BaseProcessor:
    def __init__(self, config, tokenizer=None, only_test=False):
        self.config = config
        self.tokenizer = tokenizer
        self.only_test = only_test
        self.dataset_public_path_map = {
            "wow": f"{config.public_data_path}/wizard_of_wikipedia",
            "persona_chat": f"{config.public_data_path}/persona_chat",
        }
        self.public_dataset_path = self.get_public_data_path()
        
    def get_public_data_path(self):
        return self.dataset_public_path_map[self.config.dataset]

    def get_dataset(self):
        """
        """
        if self.only_test:
            self.config.dataset_part = ['test']
        train_data_tokenized = valid_data_tokenized = test_data_tokenized = None
        if 'train' in self.config.dataset_part:
            train_rows = self.read_data(stage='train')
            train_dataset = Dataset.from_dict(train_rows)
            log.info("Tokenize Train Dataset...")
            train_data_tokenized = train_dataset.map(
                lambda batch: self.tokenize_data(batch, stage='train'),
                batched=True,
                desc='Tokenize Train Dataset'
            )
            if '__index_level_0__' in train_data_tokenized.column_names:
                train_data_tokenized = train_data_tokenized.remove_columns('__index_level_0__')

        if 'valid' in self.config.dataset_part:
            valid_rows = self.read_data(stage='valid')
            valid_dataset = Dataset.from_dict(valid_rows)
            log.info("Tokenize Valid Dataset...")
            valid_data_tokenized = valid_dataset.map(
                lambda batch: self.tokenize_data(batch, stage='valid'),
                batched=True,
                desc='Tokenize Valid Dataset'
            )
            if '__index_level_0__' in valid_data_tokenized.column_names:
                valid_data_tokenized = valid_data_tokenized.remove_columns('__index_level_0__')

        if 'test' in self.config.dataset_part:
            test_rows = self.read_data(stage='test')
            test_dataset = Dataset.from_dict(test_rows)
            log.info("Tokenize Test Dataset...")
            test_data_tokenized = test_dataset.map(
                lambda batch: self.tokenize_data(batch, stage='test'),
                batched=True,
                desc='Tokenize Test Dataset'
            )
            if '__index_level_0__' in test_data_tokenized.column_names:
                test_data_tokenized = test_data_tokenized.remove_columns('__index_level_0__')
        columns = list(train_rows.keys()) if train_data_tokenized is not None else None
        test_columns = list(test_rows.keys()) if test_data_tokenized is not None else None
        raw_data = (
            train_data_tokenized.remove_columns(
                list(set(train_data_tokenized.column_names).difference(set(columns)))) \
                if train_data_tokenized is not None else None,
            valid_data_tokenized.remove_columns(
                list(set(valid_data_tokenized.column_names).difference(set(columns)))) \
                if valid_data_tokenized is not None else None,
            test_data_tokenized.remove_columns(
                list(set(test_data_tokenized.column_names).difference(set(test_columns)))) \
                if test_data_tokenized is not None else None,
        )
        train_data_tokenized = train_data_tokenized.remove_columns(
            list(set(train_data_tokenized.column_names).intersection(set(columns)))) \
            if train_data_tokenized is not None else None
        valid_data_tokenized = valid_data_tokenized.remove_columns(
            list(set(valid_data_tokenized.column_names).intersection(set(columns)))) \
            if valid_data_tokenized is not None else None
        test_data_tokenized = test_data_tokenized.remove_columns(
            list(set(test_data_tokenized.column_names).intersection(set(test_columns)))) \
            if test_data_tokenized is not None else None

        print_dataset_overview(train_data_tokenized, valid_data_tokenized, test_data_tokenized)
        return train_data_tokenized, valid_data_tokenized, test_data_tokenized, raw_data

    def tokenize_data(self, batch, stage=None):
        pass

    def map_column(self, dataset):
        raise NotImplementedError

    def read_data(self, stage):
        """
        读取数据，根据stage【train、valid、test】获取对应的数据集
        """
        raise NotImplementedError

    def get_segment_offset(self, offset_mapping, segments, target):
        """
        根据offset_mapping获取未编码的segment在输入编码后的token ids中的下标索引
        segments: [str]
        target: str
        offset_mapping: [tuple(start_offset, end_offset)]
        """
        start_offsets = []
        end_offsets = []

        for os in offset_mapping:
            start_offsets.append(os[0])
            end_offsets.append(os[1])
        segments_index = []
        for segment in segments:
            start = 0
            while True:
                start_index = target.find(segment, start)
                if start_index < 0 or start_index not in start_offsets:
                    break
                end_index = start_index + len(segment)
                if end_index not in end_offsets:
                    break
                start_offset = start_offsets.index(start_index)
                end_offset = len(end_offsets) - end_offsets[-1::-1].index(end_index) - 1
                segments_index.append((start_offset, end_offset))

                start = end_index

        return segments_index
