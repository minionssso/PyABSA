# -*- coding: utf-8 -*-
# file: data_utils_for_inferring.py
# time: 2021/4/22 0022
# author: yangheng <yangheng@m.scnu.edu.cn>
# github: https://github.com/yangheng95
# Copyright (C) 2021. All Rights Reserved.

import numpy as np
import torch
from torch.utils.data import Dataset
from tqdm import tqdm
from .apc_utils import copy_side_aspect, is_similar, calculate_dep_dist


class ABSADataset(Dataset):

    def __init__(self, tokenizer, opt):
        self.input_colses = {
            'bert_base': ['text_raw_bert_indices'],
            'bert_spc': ['text_raw_bert_indices'],
            'lca_bert': ['text_bert_indices', 'text_raw_bert_indices', 'lca_ids', 'lcf_vec'],
            'lcf_bert': ['text_bert_indices', 'text_raw_bert_indices', 'lcf_vec'],
            'slide_lcf_bert': ['text_bert_indices', 'spc_mask_vec', 'lcf_vec', 'left_lcf_vec', 'right_lcf_vec'],
            'slide_lcfs_bert': ['text_bert_indices', 'spc_mask_vec', 'lcf_vec', 'left_lcf_vec', 'right_lcf_vec'],
            'lcfs_bert': ['text_bert_indices', 'text_raw_bert_indices', 'lcf_vec'],
        }
        self.tokenizer = tokenizer
        self.opt = opt
        self.all_data = []

    def parse_sample(self, text):
        samples = []
        try:
            if '!sent!' not in text:
                splits = text.split('[ASP]')
                for i in range(0, len(splits) - 1, 2):
                    sample = text.replace('[ASP]', '').replace(splits[i + 1], '[ASP]' + splits[i + 1] + '[ASP]')
                    samples.append(sample)
            else:
                text, ref_sent = text.split('!sent!')
                ref_sent = ref_sent.split()
                text = '[PADDING] ' + text + ' [PADDING]'
                splits = text.split('[ASP]')

                if int((len(splits) - 1) / 2) == len(ref_sent):
                    for i in range(0, len(splits) - 1, 2):
                        sample = text.replace('[ASP]' + splits[i + 1] + '[ASP]',
                                              '[TEMP]' + splits[i + 1] + '[TEMP]').replace('[ASP]', '')
                        sample += ' !sent! ' + str(ref_sent[int(i / 2)])
                        samples.append(sample.replace('[TEMP]', '[ASP]'))
                else:
                    print(text,
                          ' -> Unequal length of reference sentiment and aspects, ingore the refernece sentiment.')
                    for i in range(0, len(splits) - 1, 2):
                        sample = text.replace('[ASP]' + splits[i + 1] + '[ASP]',
                                              '[TEMP]' + splits[i + 1] + '[TEMP]').replace('[ASP]', '')
                        samples.append(sample.replace('[TEMP]', '[ASP]'))

        except:
            print('Error while processing:', text)
        return samples

    def prepare_infer_sample(self, text: str):
        self.process_data(self.parse_sample(text))

    def prepare_infer_dataset(self, infer_data_path, ignore_error):
        print('buliding word indices...')

        fin = open(infer_data_path, 'r', encoding='utf-8', newline='\n', errors='ignore')
        lines = fin.readlines()
        samples = []
        for sample in lines:
            if sample:
                samples.extend(self.parse_sample(sample))
        self.process_data(samples, ignore_error)

    def process_data(self, samples, ignore_error=True):
        all_data = []

        for text in tqdm(samples):
            try:
                # handle for empty lines in inferring dataset
                if text is None or '' == text.strip():
                    raise RuntimeError('Invalid Input!')

                # check for given polarity
                if '!sent!' in text:
                    text, polarity = text.split('!sent!')[0].strip(), text.split('!sent!')[1].strip()
                    polarity = int(polarity) + 1 if polarity else -999
                else:
                    polarity = -999
                # simply add padding in case of some aspect is at the beginning or ending of a sentence
                text_left, aspect, text_right = text.split('[ASP]')
                text_left = text_left.replace('[PADDING] ', '')
                text_right = text_right.replace(' [PADDING]', '')

                # dynamic truncation on input text
                text_left = ' '.join(
                    text_left.split(' ')[int(-(self.tokenizer.max_seq_len - len(aspect.split())) / 2) - 1:])
                text_right = ' '.join(
                    text_right.split(' ')[:int((self.tokenizer.max_seq_len - len(aspect.split())) / 2) + 1])
                text_left = ' '.join(text_left.split(' '))
                text_right = ' '.join(text_right.split(' '))
                text_raw = text_left + ' ' + aspect + ' ' + text_right

                text_bert_indices = self.tokenizer.text_to_sequence('[CLS] ' + text_raw + ' [SEP] ' + aspect + " [SEP]")
                text_raw_bert_indices = self.tokenizer.text_to_sequence("[CLS] " + text_raw + " [SEP]")
                aspect_bert_indices = self.tokenizer.text_to_sequence("[CLS] " + aspect + " [SEP]")

                if 'lca' in self.opt.model_name:
                    lca_ids, lcf_vec = self.get_lca_ids_and_cdm_vec(text_bert_indices, aspect_bert_indices, text_raw,
                                                                    aspect)
                    lcf_vec = torch.from_numpy(lcf_vec)
                    lca_ids = torch.from_numpy(lca_ids).long()
                elif 'lcf' in self.opt.model_name:
                    if 'cdm' in self.opt.lcf:
                        _, lcf_vec = self.get_lca_ids_and_cdm_vec(text_bert_indices, aspect_bert_indices, text_raw,
                                                                  aspect)
                        lcf_vec = torch.from_numpy(lcf_vec)
                    elif 'cdw' in self.opt.lcf:
                        lcf_vec = self.get_cdw_vec(text_bert_indices, aspect_bert_indices, text_raw, aspect)
                        lcf_vec = torch.from_numpy(lcf_vec)
                    elif 'fusion' in self.opt.lcf:
                        raise NotImplementedError('LCF-Fusion is not recommended due to its low efficiency!')
                    else:
                        raise KeyError('Invalid LCF Mode!')

                if 'bert_segments_ids' in self.input_colses[self.opt.model_name]:
                    aspect_indices = self.tokenizer.text_to_sequence(aspect)
                    aspect_len = np.sum(aspect_indices != 0)
                    text_raw_indices = self.tokenizer.text_to_sequence(text_raw)

                data = {
                    'text_raw': text_raw,
                    'aspect': aspect,
                    'asp_index': self.get_asp_index(text_bert_indices, aspect_bert_indices),
                    'lca_ids': lca_ids if 'lca_ids' in self.input_colses[self.opt.model_name] else 0,
                    'lcf_vec': lcf_vec if 'lcf_vec' in self.input_colses[self.opt.model_name] else 0,
                    'spc_mask_vec': self.build_spc_mask_vec(text_raw_bert_indices)
                    if 'spc_mask_vec' in self.input_colses[self.opt.model_name] else 0,
                    'text_bert_indices': text_bert_indices if 'text_bert_indices' in self.input_colses[
                        self.opt.model_name] else 0,
                    'aspect_bert_indices': aspect_bert_indices if 'aspect_bert_indices' in self.input_colses[
                        self.opt.model_name] else 0,
                    'text_raw_bert_indices': text_raw_bert_indices
                    if 'text_raw_bert_indices' in self.input_colses[self.opt.model_name] else 0,
                    'polarity': polarity,
                }

                for _, item in enumerate(data):
                    data[item] = torch.tensor(data[item]) if type(item) is not str else data[item]
                all_data.append(data)

            except Exception as e:
                print(e)
                if ignore_error:
                    print('Ignore error while processing:', text)
                else:
                    raise RuntimeError('Error while processing:', text)

        if all_data and 'slide' in self.opt.model_name:
            copy_side_aspect('left', all_data[0], all_data[0])
            for idx in range(1, len(all_data)):
                if is_similar(all_data[idx - 1]['text_bert_indices'],
                              all_data[idx]['text_bert_indices']):
                    copy_side_aspect('right', all_data[idx - 1], all_data[idx])
                    copy_side_aspect('left', all_data[idx], all_data[idx - 1])
                else:
                    copy_side_aspect('right', all_data[idx - 1], all_data[idx - 1])
                    copy_side_aspect('left', all_data[idx], all_data[idx])
            copy_side_aspect('right', all_data[-1], all_data[-1])
        self.all_data = all_data
        return all_data

    def get_lca_ids_and_cdm_vec(self, text_ids, aspect_indices, text_raw, aspect):
        SRD = self.opt.SRD
        lca_ids = np.ones((self.opt.max_seq_len), dtype=np.float32)
        cdm_vec = np.ones((self.opt.max_seq_len, self.opt.embed_dim), dtype=np.float32)
        aspect_len = np.count_nonzero(aspect_indices) - 2

        if 'lcfs' in self.opt.model_name:
            # Find distance in dependency parsing tree
            # raw_tokens, dist = calculate_dep_dist(text_spc, aspect)
            raw_tokens, dist = calculate_dep_dist(text_raw, aspect)
            raw_tokens.insert(0, self.tokenizer.cls_token)
            dist.insert(0, 0)
            raw_tokens.append(self.tokenizer.sep_token)
            dist.append(0)
            syntactical_dist = self.tokenizer.tokenize(raw_tokens, dist)[1]
            for i in range(self.opt.max_seq_len):
                if syntactical_dist[i] <= SRD:
                    lca_ids[i] = 1
                    cdm_vec[i] = np.ones((self.opt.embed_dim), dtype=np.float32)
        else:
            aspect_begin = self.get_asp_index(text_ids, aspect_indices)
            if aspect_begin < 0:
                return lca_ids, cdm_vec
            local_context_begin = max(0, aspect_begin - SRD)
            local_context_end = aspect_begin + aspect_len + SRD - 1
            for i in range(self.opt.max_seq_len):
                if local_context_begin <= i <= local_context_end:
                    lca_ids[i] = 1
                    cdm_vec[i] = np.ones((self.opt.embed_dim), dtype=np.float32)
        return lca_ids, cdm_vec

    def get_cdw_vec(self, text_ids, aspect_indices, text_raw, aspect):
        SRD = self.opt.SRD
        cdw_vec = np.zeros((self.opt.max_seq_len, self.opt.embed_dim), dtype=np.float32)
        aspect_len = np.count_nonzero(aspect_indices) - 2
        aspect_begin = np.argwhere(text_ids == aspect_indices[1])[0]
        text_len = np.flatnonzero(text_ids)[-1] + 1
        if 'lcfs' in self.opt.model_name:
            # Find distance in dependency parsing tree
            raw_tokens, dist = calculate_dep_dist(text_raw, aspect)
            raw_tokens.insert(0, self.tokenizer.cls_token)
            dist.insert(0, 0)
            raw_tokens.append(self.tokenizer.sep_token)
            dist.append(0)
            syntactical_dist = self.tokenizer.tokenize(raw_tokens, dist)[1]
            for i in range(text_len):
                if syntactical_dist[i] > SRD:
                    w = 1 - syntactical_dist[i] / text_len
                    cdw_vec[i] = w * np.ones((self.opt.embed_dim), dtype=np.float32)
                else:
                    cdw_vec[i] = np.ones((self.opt.embed_dim), dtype=np.float32)
        else:
            aspect_begin = self.get_asp_index(text_ids, aspect_indices)
            if aspect_begin < 0:
                return np.zeros((self.opt.max_seq_len, self.opt.embed_dim), dtype=np.float32)
            local_context_begin = max(0, aspect_begin - SRD)
            local_context_end = aspect_begin + aspect_len + SRD - 1
            for i in range(text_len):
                if i < local_context_begin:
                    w = 1 - (local_context_begin - i) / text_len
                    cdw_vec[i] = w * np.ones((self.opt.embed_dim), dtype=np.float32)
                elif local_context_begin <= i <= local_context_end:
                    cdw_vec[i] = np.ones((self.opt.embed_dim), dtype=np.float32)
                elif i > local_context_end:
                    w = 1 - (i - local_context_end) / text_len
                    cdw_vec[i] = w * np.ones((self.opt.embed_dim), dtype=np.float32)

        return cdw_vec

    def get_asp_index(self, text_ids, aspect_indices):
        aspect_len = np.count_nonzero(aspect_indices) - 2
        aspect_begin = np.argwhere(text_ids == aspect_indices[1])[0]
        asp_avg_index = (aspect_begin * 2 + aspect_len) / 2

        return asp_avg_index

    def build_spc_mask_vec(self, text_ids):
        spc_mask_vec = np.zeros((self.opt.max_seq_len, self.opt.embed_dim), dtype=np.float32)
        for i in range(len(text_ids)):
            if text_ids[i] != 0:
                spc_mask_vec[i] = np.ones((self.opt.embed_dim), dtype=np.float32)
        return spc_mask_vec

    def __getitem__(self, index):
        return self.all_data[index]

    def __len__(self):
        return len(self.all_data)