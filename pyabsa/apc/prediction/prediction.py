# -*- coding: utf-8 -*-
# file: functional.py
# author: yangheng <yangheng@m.scnu.edu.cn>
# Copyright (C) 2020. All Rights Reserved.

import os
import pickle
import random

import numpy
import torch
from torch.utils.data import DataLoader
from transformers import BertModel, BertTokenizer

from pyabsa.apc.models.bert_base import BERT_BASE
from pyabsa.apc.models.bert_spc import BERT_SPC
from pyabsa.apc.models.lcf_bert import LCF_BERT
from pyabsa.apc.models.slide_lcf_bert import SLIDE_LCF_BERT
from pyabsa.apc.dataset_utils.data_utils_for_inferring import ABSADataset
from pyabsa.apc.dataset_utils.apc_utils import Tokenizer4Bert
from pyabsa.pyabsa_utils import find_target_file


class SentimentClassifier:
    def __init__(self, model_arg=None):
        '''
            from_train_model: load inferring model from trained model
        '''

        self.model_class = {
            'bert_base': BERT_BASE,
            'bert_spc': BERT_SPC,
            'lcf_bert': LCF_BERT,
            'lcfs_bert': LCF_BERT,
            'slide_lcf_bert': SLIDE_LCF_BERT,
            'slide_lcfs_bert': SLIDE_LCF_BERT,
        }

        self.initializers = {
            'xavier_uniform_': torch.nn.init.xavier_uniform_,
            'xavier_normal_': torch.nn.init.xavier_normal,
            'orthogonal_': torch.nn.init.orthogonal_
        }

        # load from a model path
        if not isinstance(model_arg, str):
            print('Try to load trained model from training')
            self.model = model_arg[0]
            self.opt = model_arg[1]
        else:
            # load from a trained model
            try:
                print('Try to load trained model and config from', model_arg)
                state_dict_path = find_target_file(model_arg, 'state_dict')
                config_path = find_target_file(model_arg, 'config')
                self.opt = pickle.load(open(config_path, 'rb'))
                self.bert = BertModel.from_pretrained(self.opt.pretrained_bert_name)
                self.model = self.model_class[self.opt.model_name](self.bert, self.opt)
                self.model.load_state_dict(torch.load(state_dict_path))

                print('Config used in Training:')
                self._log_write_args()

            except Exception as e:
                print(e)
                raise RuntimeError('Fail to load the model, please download our latest models at Google Drive: '
                                   'https://drive.google.com/drive/folders/1yiMTucHKy2hAx945lgzhvb9QeHvJrStC?usp=sharing')

        self.bert_tokenizer = BertTokenizer.from_pretrained(self.opt.pretrained_bert_name, do_lower_case=True)
        self.tokenizer = Tokenizer4Bert(self.bert_tokenizer, self.opt.max_seq_len)
        self.dataset = ABSADataset(tokenizer=self.tokenizer, opt=self.opt)
        self.infer_dataloader = None

        if self.opt.seed is not None:
            random.seed(self.opt.seed)
            numpy.random.seed(self.opt.seed)
            torch.manual_seed(self.opt.seed)
            torch.cuda.manual_seed(self.opt.seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        self.opt.inputs_cols = self.dataset.input_colses[self.opt.model_name]
        self.opt.initializer = self.opt.initializer

    def to(self, device=None):
        self.opt.device = device
        self.model.to(device)

    def cpu(self):
        self.opt.device = 'cpu'
        self.model.to('cpu')

    def cuda(self, device='cuda:0'):
        self.opt.device = device
        self.model.to(device)

    def _log_write_args(self):
        n_trainable_params, n_nontrainable_params = 0, 0
        for p in self.model.parameters():
            n_params = torch.prod(torch.tensor(p.shape))
            if p.requires_grad:
                n_trainable_params += n_params
            else:
                n_nontrainable_params += n_params
        print(
            'n_trainable_params: {0}, n_nontrainable_params: {1}'.format(n_trainable_params, n_nontrainable_params))
        for arg in vars(self.opt):
            print('>>> {0}: {1}'.format(arg, getattr(self.opt, arg)))

    def batch_infer(self, test_dataset_path=None, save_result=False, clear_input_samples=True, ignore_error=True):
        if clear_input_samples:
            self.clear_input_samples()

        if os.path.isdir(test_dataset_path):
            try:
                test_dataset_path = find_target_file(test_dataset_path, 'infer')
            except Exception as e:
                raise FileNotFoundError('Can not find inference dataset!')
            self.dataset.prepare_infer_dataset(test_dataset_path, ignore_error=ignore_error)
        self.dataset.prepare_infer_dataset(test_dataset_path, ignore_error=ignore_error)
        self.infer_dataloader = DataLoader(dataset=self.dataset, batch_size=1, shuffle=False)
        return self._infer(save_path=test_dataset_path if save_result else None)

    def infer(self, text: str = None, clear_input_samples=True):
        if clear_input_samples:
            self.clear_input_samples()
        if text:
            self.dataset.prepare_infer_sample(text)
        else:
            raise RuntimeError('Please specify your dataset path!')
        self.infer_dataloader = DataLoader(dataset=self.dataset, batch_size=1, shuffle=False)
        return self._infer(print_result=True)

    def _infer(self, save_path=None, print_result=True):

        _params = filter(lambda p: p.requires_grad, self.model.parameters())
        sentiments = {0: 'Negative', 1: "Neutral", 2: 'Positive', -999: ''}
        Correct = {True: 'Correct', False: 'Wrong'}
        results = []
        if save_path:
            fout = open(save_path + '.results', 'w', encoding='utf8')
        with torch.no_grad():
            self.model.eval()
            n_correct = 0
            n_labeled = 0
            n_total = 0
            for _, sample in enumerate(self.infer_dataloader):
                result = {}
                inputs = [sample[col].to(self.opt.device) for col in self.opt.inputs_cols]
                self.model.eval()
                outputs = self.model(inputs)
                sen_logits = outputs
                t_probs = torch.softmax(sen_logits, dim=-1).cpu().numpy()
                sent = int(t_probs.argmax(axis=-1))
                real_sent = int(sample['polarity'])
                aspect = sample['aspect'][0]

                result['text'] = sample['text_raw'][0]
                result['aspect'] = sample['aspect'][0]
                result['sentiment'] = int(t_probs.argmax(axis=-1))
                result['ref_sentiment'] = sentiments[real_sent]
                result['infer result'] = Correct[sent == real_sent]
                results.append(result)
                line1 = sample['text_raw'][0]
                if real_sent == -999:
                    line2 = '{} --> {}'.format(aspect, sentiments[sent])
                else:
                    n_labeled += 1
                    if sent == real_sent:
                        n_correct += 1
                    line2 = '{} --> {}  Real Polarity: {} ({})'.format(aspect,
                                                                       sentiments[sent],
                                                                       sentiments[real_sent],
                                                                       Correct[sent == real_sent]
                                                                       )
                n_total += 1
                try:
                    if save_path:
                        fout.write(line1 + '\n')
                        fout.write(line2 + '\n')
                except:
                    raise IOError('Can not save result!')
                try:
                    if print_result:
                        print(line1)
                        print(line2)
                except:
                    raise RuntimeError('Fail to print the result!')

            print('Total samples:{}'.format(n_total))
            print('Labeled samples:{}'.format(n_labeled))
            print('Prediction Accuracy:{}%'.format(100 * n_correct / n_labeled if n_labeled else 'N.A.'))

            try:
                if save_path:
                    fout.write('Total samples:{}\n'.format(n_total))
                    fout.write('Labeled samples:{}\n'.format(n_labeled))
                    fout.write('Prediction Accuracy:{}%\n'.format(100 * n_correct / n_labeled))
            except:
                pass
        if save_path:
            fout.close()
        return results

    def clear_input_samples(self):
        self.dataset.all_data = []
