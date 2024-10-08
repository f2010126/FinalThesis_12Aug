# coding=utf-8
# 2019.12.2-Changed for TinyBERT general distillation
#      Huawei Technologies Co., Ltd. <yinyichun@huawei.com>
# Copyright 2020 Huawei Technologies Co., Ltd.
# Copyright 2018 The Google AI Language Team Authors, The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, division, print_function

import argparse
import csv
import logging
import random
import sys
import json
import torch.distributed as dist
import wandb

import torch.multiprocessing as mp
from torch.distributed import init_process_group, destroy_process_group
import os
import numpy as np
import torch
from collections import namedtuple
from tempfile import TemporaryDirectory
from pathlib import Path
from torch.utils.data import (DataLoader, RandomSampler, Dataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
from torch.nn import MSELoss
from transformers import AutoTokenizer, AutoConfig, AutoModelForMaskedLM

from transformer.file_utils import WEIGHTS_NAME, CONFIG_NAME
from transformer.modeling import TinyBertForPreTraining, BertModel
from transformer.tokenization import BertTokenizer
from transformer.optimization import BertAdam

from datetime import datetime
from torch.nn.parallel import DistributedDataParallel as DDP

# Initialize the distributed learning processes
os.environ['CURL_CA_BUNDLE'] = ''

csv.field_size_limit(sys.maxsize)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

InputFeatures = namedtuple("InputFeatures", "input_ids input_mask segment_ids lm_label_ids is_next")


def convert_example_to_features(example, tokenizer, max_seq_length):
    tokens = example["tokens"]
    segment_ids = example["segment_ids"]
    is_random_next = example["is_random_next"]
    masked_lm_positions = example["masked_lm_positions"]
    masked_lm_labels = example["masked_lm_labels"]

    if len(tokens) > max_seq_length:
        log_from_master('len(tokens): {}'.format(len(tokens)))
        log_from_master('tokens: {}'.format(tokens))
        tokens = tokens[:max_seq_length]

    if len(tokens) != len(segment_ids):
        log_from_master('tokens: {}\nsegment_ids: {}'.format(tokens, segment_ids))
        segment_ids = [0] * len(tokens)

    assert len(tokens) == len(segment_ids) <= max_seq_length  # The preprocessed data should be already truncated
    input_ids = tokenizer.convert_tokens_to_ids(tokens)
    masked_label_ids = tokenizer.convert_tokens_to_ids(masked_lm_labels)

    input_array = np.zeros(max_seq_length, dtype=int)
    input_array[:len(input_ids)] = input_ids

    mask_array = np.zeros(max_seq_length, dtype=bool)
    mask_array[:len(input_ids)] = 1

    segment_array = np.zeros(max_seq_length, dtype=bool)
    segment_array[:len(segment_ids)] = segment_ids

    lm_label_array = np.full(max_seq_length, dtype=int, fill_value=-1)
    lm_label_array[masked_lm_positions] = masked_label_ids

    features = InputFeatures(input_ids=input_array,
                             input_mask=mask_array,
                             segment_ids=segment_array,
                             lm_label_ids=lm_label_array,
                             is_next=is_random_next)
    return features


def log_from_master(msg):
    if dist.get_rank() == 0:
        logger.info(msg)


class PregeneratedDataset(Dataset):
    def __init__(self, training_path, epoch, tokenizer, num_data_epochs, reduce_memory=False):
        self.vocab = tokenizer.vocab
        self.tokenizer = tokenizer
        self.epoch = epoch
        self.data_epoch = int(epoch % num_data_epochs)
        log_from_master('training_path: {}'.format(training_path))
        data_file = training_path / "epoch_{}.json".format(self.data_epoch)
        metrics_file = training_path / "epoch_{}_metrics.json".format(self.data_epoch)

        log_from_master('data_file: {}'.format(data_file))
        log_from_master('metrics_file: {}'.format(metrics_file))

        assert data_file.is_file() and metrics_file.is_file()
        metrics = json.loads(metrics_file.read_text())
        num_samples = metrics['num_training_examples']
        seq_len = metrics['max_seq_len']
        self.temp_dir = None
        self.working_dir = None
        if reduce_memory:
            self.temp_dir = TemporaryDirectory()
            self.working_dir = Path('/cache')
            input_ids = np.memmap(filename=self.working_dir / 'input_ids.memmap',
                                  mode='w+', dtype=np.int32, shape=(num_samples, seq_len))
            input_masks = np.memmap(filename=self.working_dir / 'input_masks.memmap',
                                    shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
            segment_ids = np.memmap(filename=self.working_dir / 'segment_ids.memmap',
                                    shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
            lm_label_ids = np.memmap(filename=self.working_dir / 'lm_label_ids.memmap',
                                     shape=(num_samples, seq_len), mode='w+', dtype=np.int32)
            lm_label_ids[:] = -1
            is_nexts = np.memmap(filename=self.working_dir / 'is_nexts.memmap',
                                 shape=(num_samples,), mode='w+', dtype=np.bool)
        else:
            input_ids = np.zeros(shape=(num_samples, seq_len), dtype=np.int32)
            input_masks = np.zeros(shape=(num_samples, seq_len), dtype=bool)
            segment_ids = np.zeros(shape=(num_samples, seq_len), dtype=bool)
            lm_label_ids = np.full(shape=(num_samples, seq_len), dtype=np.int32, fill_value=-1)
            is_nexts = np.zeros(shape=(num_samples,), dtype=bool)

        log_from_master(f'Loading training examples for epoch {epoch}')

        with data_file.open() as f:
            for i, line in enumerate(tqdm(f, total=num_samples, desc="Training examples")):
                line = line.strip()
                example = json.loads(line)
                features = convert_example_to_features(example, tokenizer, seq_len)
                input_ids[i] = features.input_ids
                segment_ids[i] = features.segment_ids
                input_masks[i] = features.input_mask
                lm_label_ids[i] = features.lm_label_ids
                is_nexts[i] = features.is_next

        # assert i == num_samples - 1  # Assert that the sample count metric was true
        log_from_master("Loading complete!")
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_ids = input_ids
        self.input_masks = input_masks
        self.segment_ids = segment_ids
        self.lm_label_ids = lm_label_ids
        self.is_nexts = is_nexts

    def __len__(self):
        return self.num_samples

    def __getitem__(self, item):
        return (torch.tensor(self.input_ids[item].astype(np.int64)),
                torch.tensor(self.input_masks[item].astype(np.int64)),
                torch.tensor(self.segment_ids[item].astype(np.int64)),
                torch.tensor(self.lm_label_ids[item].astype(np.int64)),
                torch.tensor(int(self.is_nexts[item])))


def set_up_folders(folder, file_name):
    path = os.path.join(os.getcwd(), folder, file_name)
    try:
        os.makedirs(path)
    except FileExistsError:
        # directory already exists
        pass
    return path


def main(args):
    # Multi-GPU setup
    # Setup folders for checkpoints and models. If the folder already exists, the program will continue
    checkpoint_path = set_up_folders('checkpoints', args.checkpoint_name)
    model_path = set_up_folders('models', args.output_dir)
    init_process_group(backend="nccl")
    local_rank = int(os.environ["LOCAL_RANK"])
    run_logger = set_logging(args)

    # Load Data
    data_path = Path(Path.joinpath(Path.cwd(), args.pregenerated_data))
    samples_per_epoch = []
    for i in range(int(args.num_train_epochs)):
        epoch_file = data_path / "epoch_{}.json".format(i)
        metrics_file = data_path / "epoch_{}_metrics.json".format(i)
        if epoch_file.is_file() and metrics_file.is_file():
            # continue training?
            metrics = json.loads(metrics_file.read_text())
            samples_per_epoch.append(metrics['num_training_examples'])
        else:
            if i == 0:
                exit("No training data was found!")
            print("Warning! There are fewer epochs of pregenerated data ({}) than training epochs ({}).".format(i,
                                                                                                                args.num_train_epochs))
            print("This script will loop over the available data, but training diversity may be negatively impacted.")
            num_data_epochs = i
            break
    else:
        num_data_epochs = args.num_train_epochs

    if local_rank == -1 and args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = 1  # just one
    else:
        device = torch.device("cuda:{}".format(local_rank))
        n_gpu = torch.cuda.device_count()
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    log_from_master(
        f"device: {device} n_gpu: {n_gpu}, distributed training: {bool(local_rank != -1)}, 16-bits training: {args.fp16}")

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if local_rank in [-1, 0] else logging.WARN)

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    # tokenizer = BertTokenizer.from_pretrained(args.teacher_model, do_lower_case=args.do_lower_case)
    tokenizer = AutoTokenizer.from_pretrained(args.teacher_model, do_lower_case=args.do_lower_case)

    total_train_examples = 0
    for i in range(int(args.num_train_epochs)):
        # The modulo takes into account the fact that we may loop over limited epochs of data
        total_train_examples += samples_per_epoch[i % len(samples_per_epoch)]
    num_train_optimization_steps = int(
        total_train_examples / train_batch_size / args.gradient_accumulation_steps)

    if local_rank != -1:
        num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    if args.continue_train:
        student_model = TinyBertForPreTraining.from_pretrained(args.student_model)
    else:
        student_model = TinyBertForPreTraining.from_scratch(args.student_model)
    teacher_model = BertModel.from_pretrained(args.teacher_model)
    student_model.to(device)
    teacher_model.to(device)
    # Convert BatchNorm to SyncBatchNorm.type of batch normalization used for multi-GPU training.
    # Standard batch normalization only normalizes the data within each device (GPU).
    # SyncBN normalizes the input within the whole mini-batch
    student_model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(student_model)
    teacher_model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(teacher_model)

    student_model = DDP(student_model, device_ids=[local_rank], output_device=local_rank,
                        find_unused_parameters=True)
    teacher_model = DDP(teacher_model, device_ids=[local_rank], output_device=local_rank)

    size = 0
    for n, p in student_model.named_parameters():
        # logger.info('n: {}'.format(n))
        # logger.info('p: {}'.format(p.nelement()))
        size += p.nelement()

    log_from_master('Total parameters: {}'.format(size))

    # Prepare optimizer
    param_optimizer = list(student_model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    loss_mse = MSELoss()
    optimizer = BertAdam(optimizer_grouped_parameters,
                         lr=args.learning_rate,
                         warmup=args.warmup_proportion,
                         t_total=num_train_optimization_steps)

    global_step = 0
    ep_step = 0
    epochs_run = 0
    snapshot_path = os.path.join(checkpoint_path, 'resume_checkpoint.pt')
    if os.path.exists(snapshot_path):
        print("Loading snapshot from {}".format(snapshot_path))
        loc = f"cuda:{local_rank}"
        snapshot = torch.load(snapshot_path, map_location=loc)
        student_model.load_state_dict(snapshot["student_model_state_dict"])
        teacher_model.load_state_dict(snapshot["teacher_model_state_dict"])
        epochs_run = snapshot["epoch"]
        global_step = snapshot["global_step"]
        ep_step = snapshot["ep_step"]
        optimizer.load_state_dict(snapshot["optimizer_state_dict"])

    if local_rank == 0:
        run_logger.watch(student_model)

    log_from_master(f'args:{args}')
    log_from_master("***** Running training *****")
    log_from_master(f"  Num examples = {total_train_examples}")
    log_from_master(f"  Per GPU Batch size ={train_batch_size}")
    log_from_master(f" Overall? Num steps = {num_train_optimization_steps}")

    # the original will have 0_0, the resuming will have something else
    logging_file = "{}_{}_log.txt".format(epochs_run, ep_step)
    log_from_master(f"logging file: {logging_file} ")
    # General Distillation
    for epoch in trange(epochs_run, int(args.num_train_epochs), desc="Epoch"):
        epoch_dataset = PregeneratedDataset(epoch=epoch, training_path=args.pregenerated_data, tokenizer=tokenizer,
                                            num_data_epochs=num_data_epochs, reduce_memory=args.reduce_memory)
        if local_rank == -1:
            train_sampler = RandomSampler(epoch_dataset)
        else:
            # distributed sampler for multi-gpu training
            train_sampler = DistributedSampler(epoch_dataset)
        train_dataloader = DataLoader(epoch_dataset, sampler=train_sampler, batch_size=train_batch_size,
                                      shuffle=False, )
        #  make shuffling work properly across multiple epochs. 
        train_dataloader.sampler.set_epoch(epoch)
        # running loss values for the epoch
        tr_loss = 0.
        tr_att_loss = 0.
        tr_rep_loss = 0.
        student_model.train()
        nb_tr_examples, nb_tr_steps = 0, 0
        with tqdm(total=len(train_dataloader), desc="Epoch {}".format(epoch)) as pbar:
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration", ascii=True)):

                # restarting from checkpoint
                if step < ep_step:
                    continue

                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, lm_label_ids, is_next = batch
                if input_ids.size()[0] != train_batch_size:
                    continue

                att_loss = 0.
                rep_loss = 0.

                student_atts, student_reps = student_model(input_ids, segment_ids, input_mask)
                teacher_reps, teacher_atts, _ = teacher_model(input_ids, segment_ids, input_mask)
                teacher_reps = [teacher_rep.detach() for teacher_rep in teacher_reps]  # speedup 1.5x
                teacher_atts = [teacher_att.detach() for teacher_att in teacher_atts]

                teacher_layer_num = len(teacher_atts)
                student_layer_num = len(student_atts)
                log_from_master(f"teacher_layer_num: {teacher_layer_num}, student_layer_num: {student_layer_num}")
                assert teacher_layer_num % student_layer_num == 0
                layers_per_block = int(teacher_layer_num / student_layer_num)
                log_from_master(f"layers_per_block: {layers_per_block}")
                new_teacher_atts = [teacher_atts[i * layers_per_block + layers_per_block - 1]
                                    for i in range(student_layer_num)]

                for student_att, teacher_att in zip(student_atts, new_teacher_atts):
                    student_att = torch.where(student_att <= -1e2, torch.zeros_like(student_att).to(device),
                                              student_att)
                    teacher_att = torch.where(teacher_att <= -1e2, torch.zeros_like(teacher_att).to(device),
                                              teacher_att)
                    att_loss += loss_mse(student_att, teacher_att)

                    del student_att, teacher_att

                # selects from the first layer of each block
                new_teacher_reps = [teacher_reps[i * layers_per_block] for i in range(student_layer_num + 1)]
                new_student_reps = student_reps

                for student_rep, teacher_rep in zip(new_student_reps, new_teacher_reps):
                    rep_loss += loss_mse(student_rep, teacher_rep)
                    del student_rep, teacher_rep

                loss = (args.attn_scale * att_loss) + (args.rep_scale * rep_loss)
                log_from_master(f"LOSS FOR THAT BATCH att_loss: {att_loss}, rep_loss: {rep_loss}, loss: {loss}")
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                log_from_master(f"scaled loss by {args.gradient_accumulation_steps}----->: {loss}")

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()

                tr_att_loss += att_loss.item()
                tr_rep_loss += rep_loss.item()

                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                pbar.update(1)

                mean_loss = tr_loss * args.gradient_accumulation_steps / nb_tr_steps
                mean_att_loss = tr_att_loss * args.gradient_accumulation_steps / nb_tr_steps
                mean_rep_loss = tr_rep_loss * args.gradient_accumulation_steps / nb_tr_steps

                log_from_master(f"RUNNING LOSS mean_loss: {mean_loss}, mean_att_loss: {mean_att_loss}, mean_rep_loss: {mean_rep_loss})")

                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1

                # Logging and evaluation in main process
                log_from_master(f"local_ rank  {local_rank} global_step: {global_step}")
                if local_rank == 0:

                    # Save a trained model
                    if step % 10 == 0:
                        model_name = "step_{}_{}".format(global_step, WEIGHTS_NAME)
                        logging.info("** ** * Saving tinBERT model Eval Step** ** * ")
                        # Only save the model it-self
                        model_to_save = student_model.module if hasattr(student_model, 'module') else student_model

                        output_model_file = os.path.join(model_path, model_name)
                        output_config_file = os.path.join(model_path, CONFIG_NAME)

                        torch.save(model_to_save.state_dict(), output_model_file)
                        model_to_save.config.to_json_file(output_config_file)
                        tokenizer.save_vocabulary(model_path)

                        ckpt_path = os.path.join(checkpoint_path, 'resume_checkpoint.pt')
                        torch.save({
                            'ep_step': step,
                            'epoch': epoch,
                            'global_step': global_step,
                            'student_model_state_dict': student_model.state_dict(),
                            'teacher_model_state_dict': teacher_model.state_dict(),
                            'optimizer_state_dict': optimizer.state_dict(),
                        }, ckpt_path)

                    # Log eval results
                    if (global_step + 1) % args.eval_step == 0:
                        result = {}
                        result['global_step'] = global_step
                        result['loss'] = mean_loss
                        result['att_loss'] = mean_att_loss
                        result['rep_loss'] = mean_rep_loss

                        run_logger.log(result)

                        output_eval_file = os.path.join(model_path, logging_file)
                        with open(output_eval_file, "a") as writer:
                            log_from_master("***** Eval results *****")
                            for key in sorted(result.keys()):
                                log_from_master(f"{key} = {str(result[key])}")
                                writer.write("%s = %s\n" % (key, str(result[key])))
                        # save to checkpoints
                        output_eval_file = os.path.join(checkpoint_path, logging_file)
                        with open(output_eval_file, "a") as writer:
                            log_from_master("***** Eval results *****")
                            for key in sorted(result.keys()):
                                log_from_master(f"{key} = {str(result[key])}")
                                writer.write("%s = %s\n" % (key, str(result[key])))

                        run_logger.save(output_eval_file)

            # Save a trained model only for master process
            if local_rank == 0:
                model_name = "step_{}_{}".format(global_step, WEIGHTS_NAME)
                logging.info("** ** * Saving fine-tuned model Final ** ** * ")
                model_to_save = student_model.module if hasattr(student_model, 'module') else student_model

                output_model_file = os.path.join(model_path, model_name)
                output_config_file = os.path.join(model_path, CONFIG_NAME)

                torch.save(model_to_save.state_dict(), output_model_file)
                model_to_save.config.to_json_file(output_config_file)
                tokenizer.save_vocabulary(model_path)

    # Save a trained model only for master process
    if local_rank == 0:
        model_name = "gen_distill_tinyBERT_{}".format(WEIGHTS_NAME)
        logging.info("** ** * Saving fine-tuned model Final ** ** * ")
        model_to_save = student_model.module if hasattr(student_model, 'module') else student_model

        output_model_file = os.path.join(model_path, model_name)
        output_config_file = os.path.join(model_path, CONFIG_NAME)

        torch.save(model_to_save.state_dict(), output_model_file)
        model_to_save.config.to_json_file(output_config_file)
        tokenizer.save_vocabulary(model_path)

    destroy_process_group()


def set_logging(args):
    if dist.get_rank() == 0:
        run = wandb.init(
            entity='insane_gupta',
            project=args.exp_name,
            group=args.group_name,
            job_type=args.job_name,
            config=args,
        )
        return run
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--pregenerated_data",
                        type=Path,
                        default=Path('data/pretraining_data'), )
    parser.add_argument("--teacher_model",
                        type=str,
                        default="bert-base-german-dbmdz-cased")
    parser.add_argument("--student_model",
                        type=str,
                        default="bert-base-german-dbmdz-cased")
    # models stored models/output_dir
    parser.add_argument("--output_dir",
                        type=str,
                        default='name_exp_models')
    # checkpoints stored checkpoints/checkpoint_name
    parser.add_argument("--checkpoint-name",
                        type=str,
                        default='name_exp_checkpoints')

    # Other parameters
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")

    parser.add_argument("--reduce_memory",
                        action="store_true",
                        help="Store training data as on-disc memmaps to massively reduce memory usage")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=128,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument('--weight_decay',
                        '--wd',
                        default=1e-4,
                        type=float, metavar='W',
                        help='weight decay')
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local-rank",
                        type=int,
                        default=0,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    # 4 GPUS use 8 as gradient accumulation steps, 8 GPUS use 16 as gradient accumulation steps
    parser.add_argument('--gradient_accumulation_steps',
                        default=16,
                        type=int,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--continue_train',
                        action='store_true',
                        help='Whether to train from checkpoints')

    # Additional arguments
    parser.add_argument('--eval_step',
                        type=int,
                        default=1000)
    parser.add_argument('--attn_scale',
                        type=float, help='how much to scale the attention loss',
                        default=1.0)
    parser.add_argument('--rep_scale',
                        type=float, help='how much to scale the representation loss',
                        default=1.0)
    # Logging parameters
    parser.add_argument("--exp_name", type=str, help="Name of WANDDB experiment.", default="Test_TinyBERT-DE")
    parser.add_argument("--group_name", type=str, help="Name of WANDDB group.", default="test_general-distillation")
    parser.add_argument("--job_name", type=str, help="Name of WANDDB job.", default="8GPU")

    args = parser.parse_args()
    main(args)
