import os
from torch.utils.data import Dataset
import numpy as np
import random
import json
import torch
from transformers import AutoTokenizer, BatchEncoding
from dataclasses import dataclass
import cv2
import albumentations as A
from albumentations.pytorch import ToTensorV2
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union, Literal
from deepfashion.utils.utils import *
from deepfashion.datasets.processor import *

@dataclass
class DatasetArguments:
    polyvore_split: str = 'nondisjoint'
    task_type: str = 'cp'
    dataset_type: str = 'train'
    outfit_max_length: int = 8
    img_size: int = 224
    img_transform: Optional[List[Any]] = None
    txt_type: Literal['hglmm', 'token'] = 'token'
    txt_max_token: int = 16
    n_neg: int = 4
    

class PolyvoreDataset(Dataset):

    def __init__(
            self,
            data_dir: str,
            args: DatasetArguments,
            tokenizer: Optional[AutoTokenizer] = None
            ):
        # Arguments
        self.args = args
        self.is_train = (args.dataset_type == 'train')
        # Meta Data preprocessing
        self.item_ids, self.item_id2idx, \
        self.item_id2category, self.category2item_ids, self.categories, \
            self.outfit_id2item_id, self.item_id2desc = load_data(data_dir, args)
        
        # Data Configurations
        self.img_dir = os.path.join(data_dir, 'images')
        use_custom_transform = True if args.img_transform else False
        self.image_processor = DeepFashionImageProcessor(size=args.img_size, use_custom_transform=use_custom_transform, custom_transform=args.img_transform)
        if args.txt_type == 'hglmm':
            self.desc2hglmm = load_hglmm(data_dir, args)
            self.input_processor = DeepFashionInputProcessor(categories=self.categories, use_text_feature=True, text_feature_dim=6000, outfit_max_length=args.outfit_max_length)
        else:
            self.input_processor = DeepFashionInputProcessor(categories=self.categories, image_processor=self.image_processor, text_tokenizer=tokenizer, text_max_length=args.txt_max_token, outfit_max_length=args.outfit_max_length)
        
        # Input
        if args.task_type == 'cp':
            self.data = load_cp_inputs(data_dir, args, self.outfit_id2item_id)
        elif args.task_type == 'fitb':
            self.data = load_fitb_inputs(data_dir, args, self.outfit_id2item_id)
        elif args.task_type in ['triplet', 'n-pair']:
            self.data = load_triplet_inputs(data_dir, args, self.outfit_id2item_id)
        else:
            raise ValueError('task_type must be one of "cp", "fitb", and "triplet".')

    def _load_img(self, item_id):
        path = os.path.join(self.img_dir, f"{item_id}.jpg")
        img = cv2.imread(path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img
    
    def _load_txt(self, item_id):
        desc = self.item_id2desc[item_id] if item_id in self.item_id2desc else self.item_id2category[item_id]
        if self.args.txt_type == 'hglmm':
            if desc in self.desc2hglmm.keys():
                desc = self.desc2hglmm[desc]
            else:
                desc = np.zeros(self.text_feat_dim, np.float32)
        return desc
    
    def _get_single_input(self, item_id):
        category = self.item_id2category[item_id]
        img = self._load_img(item_id)
        txt = self._load_txt(item_id)
        return category, img, txt
    
    def _get_inputs(self, item_ids, pad: bool=False) -> Dict[Literal['input_mask', 'img', 'desc'], Tensor]:
        category, images, texts = map(lambda x: list(x), zip(*[self._get_single_input(item_id) for item_id in item_ids]))

        return self.input_processor(category, images, texts, do_pad=pad)

    def _get_neg_samples(self, positive_id, n, ignore_ids=None):
        return  random.sample(self.item_ids, n)

    def __getitem__(self, idx):
        if self.args.task_type == 'cp':
            target, outfit_ids = self.data[idx]
            inputs = self._get_inputs(outfit_ids, pad=True)
            return {'target': target, 'inputs': inputs}
        
        elif self.args.task_type =='fitb':
            question_ids, candidate_ids = self.data[idx]
            questions = self._get_inputs(question_ids, pad=True)
            candidates = self._get_inputs(candidate_ids)
            return  {'questions': questions, 'candidates': candidates} # ans is always 0 index
        
        elif self.args.task_type =='triplet':
            outfit_ids = self.data[idx].copy()
            random.shuffle(outfit_ids)
            
            positive_ids = [outfit_ids.pop()]
            anchor_ids = [outfit_ids.pop()]
            negative_ids = self._get_neg_samples(positive_ids, n=self.args.n_neg, ignore_ids=anchor_ids)

            anchors = self._get_inputs(anchor_ids, pad=True)
            positives = self._get_inputs(positive_ids)
            negatives = self._get_inputs(negative_ids)
                
            return {'anchors': anchors, 'positives': positives, 'negatives': negatives}
        
        elif self.args.task_type =='n-pair':
            outfit_ids = self.data[idx].copy()
            random.shuffle(outfit_ids)
            
            positive_ids = [outfit_ids.pop()]
            anchor_ids = outfit_ids
            negative_ids = self._get_neg_samples(positive_ids, n=self.args.n_neg, ignore_ids=anchor_ids)

            anchors = self._get_inputs(anchor_ids, pad=True)
            positives = self._get_inputs(positive_ids)
            negatives = self._get_inputs(negative_ids)
                
            return {'anchors': anchors, 'positives': positives, 'negatives': negatives}

    def __len__(self):
        return len(self.data)
    


def load_fitb_inputs(data_dir, args, outfit_id2item_id):
    fitb_path = os.path.join(data_dir, args.polyvore_split, f'fill_in_blank_{args.dataset_type}.json')
    with open(fitb_path, 'r') as f:
        fitb_data = json.load(f)
        fitb_inputs = []
        for item in fitb_data:
            question_ids = list(map(lambda x: outfit_id2item_id[x], item['question'][:args.outfit_max_length]))
            candidate_ids = list(map(lambda x: outfit_id2item_id[x], item['answers']))
            fitb_inputs.append((question_ids, candidate_ids))
    return fitb_inputs


def load_cp_inputs(data_dir, args, outfit_id2item_id):
    cp_path = os.path.join(data_dir, args.polyvore_split, f'compatibility_{args.dataset_type}.txt')
    with open(cp_path, 'r') as f:
        cp_data = f.readlines()
        cp_inputs = []
        for d in cp_data:
            target, *item_ids = d.split()
            cp_inputs.append((torch.FloatTensor([int(target)]), list(map(lambda x: outfit_id2item_id[x], item_ids[:args.outfit_max_length]))))
    return cp_inputs


def load_triplet_inputs(data_dir, args, outfit_id2item_id):
    outfit_data_path = os.path.join(data_dir, args.polyvore_split, f'{args.dataset_type}.json')
    outfit_data = json.load(open(outfit_data_path))
    triplet_inputs = [[outfit['items'][i]['item_id'] 
                       for i in range(min(len(outfit['items']), args.outfit_max_length))] 
                       for outfit in outfit_data]
    triplet_inputs = list(filter(lambda x: len(x) > 1, triplet_inputs))
    return triplet_inputs


def load_hglmm(data_dir, args):
    txt_dim = 6000
    txt = os.path.join(data_dir, args.polyvore_split, 'train_hglmm_pca6000.txt')
    desc2hglmm = {}
    with open(txt, 'r') as f:
        for line in tqdm(f):
            line = line.strip().split(',')
            if not line:
                continue
            desc = ','.join(line[:-txt_dim])
            vec = np.array([float(x) for x in line[-txt_dim:]], np.float32)
            desc2hglmm[desc] = vec
    return desc2hglmm


def load_data(data_dir, args):
    # Paths
    # data_dir = os.path.join(data_dir, args.polyvore_split)
    outfit_data_path = os.path.join(data_dir, args.polyvore_split, f'{args.dataset_type}.json')
    meta_data_path = os.path.join(data_dir, 'polyvore_item_metadata.json')
    outfit_data = json.load(open(outfit_data_path))
    meta_data = json.load(open(meta_data_path))
    # Load
    item_ids = set()
    categories = set()
    item_id2category = {}
    item_id2desc = {}
    category2item_ids = {}
    outfit_id2item_id = {}
    for outfit in outfit_data:
        outfit_id = outfit['set_id']
        for item in outfit['items']:
            # Item of cloth
            item_id = item['item_id']
            # Category of cloth
            category = meta_data[item_id]['semantic_category']
            categories.add(category)
            item_id2category[item_id] = category
            if category not in category2item_ids:
                category2item_ids[category] = set()
            category2item_ids[category].add(item_id)
            # Description of cloth
            desc = meta_data[item_id]['title']
            if not desc:
                desc = meta_data[item_id]['url_name']
            item_id2desc[item_id] = desc.replace('\n','').strip().lower()
            # Replace the item code with the outfit number with the image code
            outfit_id2item_id[f"{outfit['set_id']}_{item['index']}"] = item_id
            item_ids.add(item_id)
    item_ids = list(item_ids)
    item_id2idx = {id : idx for idx, id in enumerate(item_ids)}
    categories = ['<PAD>'] + list(categories)

    return item_ids, item_id2idx, \
        item_id2category, category2item_ids, categories, \
            outfit_id2item_id, item_id2desc