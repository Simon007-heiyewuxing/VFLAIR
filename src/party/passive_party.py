import sys, os
sys.path.append(os.pardir)
import torch
import json
import collections
from torch.utils.data import DataLoader
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence

from utils.basic_functions import cross_entropy_for_onehot, tf_distance_cov_cor
from party.party import Party
from party.llm_party import Party as Party_LLM

from dataset.party_dataset import PassiveDataset, PassiveDataset_LLM
from dataset.party_dataset import ActiveDataset
from load.LoadModels import load_models_per_party, load_models_per_party_new, QuestionAnsweringModelOutput

from utils.squad_utils import normalize_answer, _get_best_indexes, compute_exact, compute_f1
from utils.communication_protocol_funcs import get_size_of
from evaluates.defenses.defense_api import apply_defense
from dataset.party_dataset import ActiveDataset
from utils.communication_protocol_funcs import compress_pred

from models.imagined_adversary_models import *
from models.adversarial_model import *

from models.mid_model_rapper import *

import time
import numpy as np
from .LocalCommunication import LocalCommunication


class PassiveParty(Party):
    def __init__(self, args, index):
        super().__init__(args, index)

    def prepare_data(self, args, index):
        super().prepare_data(args, index)
        # self.train_dst = TensorDataset(train_inputs, train_masks) # the second label is just a place holder
        # self.test_dst = TensorDataset(test_inputs, test_masks) # the second label is just a place holder
        
        self.train_dst = PassiveDataset(self.train_data)
        self.test_dst = PassiveDataset(self.test_data)
        if self.args.need_auxiliary == 1:
            self.aux_dst = ActiveDataset(self.aux_data, self.aux_label)


class PassiveParty_LLM(Party_LLM):
    _communication = None

    def __init__(self, args, index, need_data = True):
        super().__init__(args, index, need_data = need_data)
        print(f'==== initialize PassiveParty_LLM : party {index}======')
        
        self.init_apply_defense(args.apply_defense, args.apply_adversarial, args.defense_configs, args.main_lr, args.device)
        if args.device == 'cuda':
            cuda_id = args.gpu
            torch.cuda.set_device(cuda_id)
            print(f'running on cuda{torch.cuda.current_device()}')
        self.criterion = cross_entropy_for_onehot
        
        # self.encoder = args.encoder
        self.train_index = None #args.idx_train
        self.test_index = None #args.idx_test
        
        self.device = args.device

        self.gt_one_hot_label = None
        self.clean_one_hot_label = None

        self.pred_received = []
        for _ in range(args.k):
            self.pred_received.append([])
        
        self.global_pred = None
        self.global_loss = None
        self.communication_cost = 0
        self.num_total_comms = 0
        self.current_step = 0

        self.num_labels = args.num_classes
        self.weights_grad_a = None # no gradient for model in passive party(no model update)

        self.encoder_trainable = args.encoder_trainable[index]
    # def prepare_model(self, args, index):
    #     (
    #         args,
    #         self.local_model,
    #         self.local_model_optimizer,
    #         self.global_model,
    #         self.global_model_optimizer
    #     ) = load_models_per_party(args, index)
        

    #     ######## some defense models, add here ########
    #     if args.apply_defense == True:
    #         if self.args.apply_adversarial and (self.index in self.args.defense_configs["party"]):
    
    def init_communication(self, communication=None):
        if communication is None:
            communication = LocalCommunication(self.args.parties[self.args.k - 1])
        self._communication = communication

    def init_apply_defense(self, need_apply_defense, apply_adversarial, defense_configs, main_lr, device):
        # some defense need model, add here
        if need_apply_defense:
            if apply_adversarial and (self.index in defense_configs["party"]):
                # add adversarial model for local model
                if not 'party' in defense_configs:
                    defense_configs['party'] = [0]
                    print('[warning] default passive party selected for applying adversarial training')

            
                self.adversarial_model_lr = defense_configs['adversarial_model_lr']

                if not ('adversarial_model' in defense_configs):
                    adversarial_model_name = 'Adversarial_Mapping'
                else:
                    adversarial_model_name = defense_configs['adversarial_model']

                seq_length = defense_configs['seq_length']
                embed_dim = defense_configs['embed_dim']
                
                # prepare adversarial model --  for adversarial training
                self.adversarial_model = globals()[adversarial_model_name](seq_length, embed_dim).to(self.args.device)
                if self.local_model_optimizer == None:
                    self.local_model_optimizer = torch.optim.Adam(self.adversarial_model.parameters(), lr=self.adversarial_model_lr)
                else:
                    self.local_model_optimizer.add_param_group({'params': self.adversarial_model.parameters(), 'lr': self.adversarial_model_lr})

                # self.adversarial_model_optimizer = torch.optim.Adam(
                #             [{'params': self.adversarial_model.parameters(), 'lr': adversarial_model_lr}])

                # prepare imagined adversary --  for adversarial training
                imagined_adversary_model_name = defense_configs['imagined_adversary']
                self.imagined_adversary = globals()[imagined_adversary_model_name](seq_length, embed_dim).to(device)
                self.imagined_adversary_lr = defense_configs['imagined_adversary_lr']
                self.imagined_adversary_optimizer = torch.optim.Adam(list(self.imagined_adversary.parameters()), lr=self.imagined_adversary_lr)

                self.adversary_crit = nn.CrossEntropyLoss()
                self.adversary_lambda = defense_configs['lambda']
            
            elif self.args.apply_mid and (self.index in self.args.defense_configs["party"]):
                self.mid_lambda = self.args.defense_configs['lambda'] 
                self.mid_model_name = self.args.defense_configs['mid_model_name'] 
                self.mid_lr = self.args.defense_configs['lr'] 

                self.mid_position = self.args.defense_configs['mid_position'] \
                if 'mid_position' in self.args.defense_configs else "out" # "inner"

                current_bottleneck_scale = int(self.args.defense_configs['bottleneck_scale']) \
                    if 'bottleneck_scale' in self.args.defense_configs else 1
        
                if 'std_shift_hyperparameter' in self.args.defense_configs:
                    std_shift_hyperparameter = int(self.args.defense_configs['std_shift_hyperparameter'])
                else:
                    std_shift_hyperparameter = 5 
                    

                seq_length = self.args.defense_configs['seq_length']
                embed_dim = self.args.defense_configs['embed_dim']


                if self.mid_position == "inner":
                    print('init defense: inner mid')
                    self.local_model.inner_mid_model = globals()[self.mid_model_name](seq_length,embed_dim,\
                    mid_lambda=self.mid_lambda,bottleneck_scale=current_bottleneck_scale, std_shift=std_shift_hyperparameter).to(self.args.device)
                    
                    if self.local_model_optimizer == None:
                        self.local_model_optimizer = torch.optim.Adam(self.local_model.inner_mid_model.parameters(), lr=self.mid_lr)
                    else:
                        self.local_model_optimizer.add_param_group({'params': self.local_model.inner_mid_model.parameters(),\
                         'lr': self.mid_lr})

                else:
                    print('init defense: out mid')
                    self.mid_model = globals()[self.mid_model_name](seq_length,embed_dim,\
                    mid_lambda=self.mid_lambda,bottleneck_scale=current_bottleneck_scale, std_shift=std_shift_hyperparameter).to(self.args.device)

                    if self.local_model_optimizer == None:
                        self.local_model_optimizer = torch.optim.Adam(self.mid_model.parameters(), lr=self.mid_lr)
                    else:
                        self.local_model_optimizer.add_param_group({'params': self.mid_model.parameters(), 'lr': self.mid_lr})

                print(f'self.mid_model_name:{self.mid_model_name}')


    def prepare_data(self, args, index):
        super().prepare_data(args, index) # Party_llm's prepare_data
 
        self.train_dst = PassiveDataset_LLM(args, self.train_data, self.train_label)
        self.test_dst = PassiveDataset_LLM(args, self.test_data, self.test_label)

    # def prepare_data_loader(self):
    #     super().prepare_data_loader(self.args.batch_size, self.args.need_auxiliary)
            
    def update_local_pred(self, pred):
        self.pred_received[self.args.k-1] = pred
    
    def receive_pred(self, pred, giver_index):
        self.pred_received[giver_index] = pred

    def cal_global_gradient(self, global_loss, global_pred):
        # print('Passive Party cal global_gradients:')
        # print('Global Loss=',global_loss)

        if self.args.task_type == 'QuestionAnswering':
            _gradients_start = torch.autograd.grad(global_loss, global_pred.start_logits, retain_graph=True)
            _gradients_end = torch.autograd.grad(global_loss, global_pred.end_logits, retain_graph=True)
            global_gradients = _gradients_end+_gradients_start
            global_gradients_clone = global_gradients[0].detach().clone()
            global_gradients_clone = global_gradients_clone/2
            self.global_gradients = global_gradients_clone
        else:
            global_gradients = torch.autograd.grad(global_loss, global_pred, retain_graph=True)
            global_gradients_clone = global_gradients[0].detach().clone()
            self.global_gradients = global_gradients_clone

        return global_gradients_clone

    def cal_loss(self, pred, test=False):
        gt_one_hot_label = self.gt_one_hot_label # label
        
        # ########### Normal Loss ###############
        if self.args.task_type == 'SequenceClassification':
            # loss = self.criterion(pred, gt_one_hot_label)
            pooled_logits = pred
            labels = gt_one_hot_label

            # GPT2
            if self.num_labels == 1:
                self.problem_type = "regression"
            else:
                self.problem_type = "single_label_classification"
            # elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
            #     self.problem_type = "single_label_classification"
            # else:
            #     self.problem_type = "multi_label_classification"

            # print('self.problem_type:',self.problem_type)
            # print('labels:',labels.dtype, labels.shape)

            if self.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(pooled_logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(pooled_logits, labels)
            elif self.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(pooled_logits.view(-1, self.num_labels), labels) #labels.view(-1)
            # elif self.problem_type == "multi_label_classification":
            #     loss_fct = BCEWithLogitsLoss()
            #     loss = loss_fct(pooled_logits, labels)

        elif self.args.task_type == 'CausalLM':
            #  GPT2
            labels = gt_one_hot_label
            # print('labels:',type(labels),labels)  # list of target_tokens
            label_id = [ self.args.tokenizer.convert_tokens_to_ids( label_text ) for label_text in labels ]
            label_id = torch.tensor(label_id).to(self.args.device)
            # print('label_id:', label_id.shape ) # torch.size([bs])
            
            lm_logits = pred # # [bs, seq_len, vocab_size]
            next_token_logits = lm_logits[:,-1,:]
            # print('next_token_logits:',next_token_logits.shape) # [bs, vocab_size]
            
            # Shift so that tokens < n predict n
            # shift_logits = lm_logits[..., :-1, :].contiguous()
            # print('shift_logits:',shift_logits.shape)
            # shift_labels = label_id #labels[..., 1:].contiguous()

            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            # loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
            loss = loss_fct(next_token_logits, label_id)
            # print('loss:', loss)

        elif self.args.task_type == 'QuestionAnswering':
            # GPT2
            # print('gt_one_hot_label:',type(gt_one_hot_label), gt_one_hot_label.shape) # torch.size( bs, 2)
            # print('gt_one_hot_label[0]:',gt_one_hot_label[0].shape)
            start_logits = pred.start_logits
            end_logits = pred.end_logits

            # golden_start_positions, golden_end_positions = gt_one_hot_label[0] # bs *[start_id, end_id]  bs=1
            
            golden_start_positions = torch.tensor( [gt_one_hot_label[i][0] for i in range(gt_one_hot_label.shape[0])] )
            golden_end_positions = torch.tensor( [gt_one_hot_label[i][1] for i in range(gt_one_hot_label.shape[0])] )

            golden_start_positions = golden_start_positions.squeeze().long().to(start_logits.device) # .unsqueeze(0)
            golden_end_positions = golden_end_positions.squeeze().long().to(end_logits.device)
            # print('golden_start_positions golden_end_positions:',golden_start_positions.shape, golden_end_positions.shape)

            loss = None

            if len(golden_start_positions.size()) > 1:
                golden_start_positions = golden_start_positions.squeeze(-1).to(start_logits.device)
            if len(golden_end_positions.size()) > 1:
                golden_end_positions = golden_end_positions.squeeze(-1).to(end_logits.device)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            # print('ignored_index:',ignored_index)
            golden_start_positions = golden_start_positions.clamp(0, ignored_index)
            golden_end_positions = golden_end_positions.clamp(0, ignored_index)

            # print('start_logits end_logits:',start_logits.shape, end_logits.shape)
            # print('after clamp golden_start_positions golden_end_positions:',golden_start_positions.shape, golden_end_positions.shape)
            
            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)

            start_loss = loss_fct(start_logits, golden_start_positions)
            end_loss = loss_fct(end_logits, golden_end_positions)
            loss = (start_loss + end_loss) / 2

            # print('start_loss:',start_loss,' end_loss:',end_loss,' loss:',loss)

        else:
            assert 1>2 , 'Task type not supported'
        
        self.global_loss = loss

        # ########### Defense on Loss ###############
        if self.args.apply_adversarial and (self.index in self.args.defense_configs["party"]):

            intermediate = self.local_pred # pred after adversarial model: bs, seq, embed_dim768
            adversary_recovered_embedding = self.imagined_adversary(intermediate)
            real_embedding =  self.local_model.embedding_output
            self.adversary_attack_loss = self.adversary_crit(adversary_recovered_embedding, real_embedding) / intermediate.shape[0]
            
            mapping_distance = torch.norm( self.origin_pred - self.local_pred , p=2)
            print(f'main_loss={self.global_loss},mapping_distance={mapping_distance},adversary_attack_loss={self.adversary_attack_loss}')

            # renew global loss function : loss used to update adversarial model mapping
            self.adversarial_model_loss =   self.adversary_lambda * mapping_distance  - self.adversary_attack_loss
            self.global_loss = self.global_loss + self.adversarial_model_loss

        elif self.args.apply_mid == True and (self.index in self.args.defense_configs['party']):
            # print(f'main_loss={self.global_loss},mid_loss={self.mid_loss}')
            # print('self.mid_loss.requires_grad:',self.mid_loss.requires_grad)
            self.global_loss = self.global_loss + self.mid_loss
        # ########### Defense on Loss ###############

        return self.global_loss

    def gradient_calculation(self, pred_list, loss):
        pred_gradients_list = []
        pred_gradients_list_clone = []
        for ik in range(self.args.k):
            pred_gradients_list.append(torch.autograd.grad(loss, pred_list[ik], retain_graph=True, create_graph=True))
            # print(f"in gradient_calculation, party#{ik}, loss={loss}, pred_gradeints={pred_gradients_list[-1]}")
            pred_gradients_list_clone.append(pred_gradients_list[ik][0].detach().clone())
        # self.global_backward(pred, loss)
        return pred_gradients_list, pred_gradients_list_clone
    
    def update_local_gradient(self, gradient):
        self.local_gradient = gradient

    def global_LR_decay(self,i_epoch):
        if self.global_model_optimizer != None: 
            eta_0 = self.args.main_lr
            eta_t = eta_0/(np.sqrt(i_epoch+1))
            for param_group in self.global_model_optimizer.param_groups:
                param_group['lr'] = eta_t

    def local_backward(self):
        # print(' === passive local backward === ')

        self.num_local_updates += 1 # another update

        # adversarial training : update adversarial model
        if (self.args.apply_adversarial == True and (self.index in self.args.defense_configs["party"])):
            # imagined_adversary update
            self.imagined_adversary_optimizer.zero_grad()
            self.adversary_attack_loss.backward(retain_graph = True)
            self.imagined_adversary_optimizer.step()

            self.local_model_optimizer.zero_grad()

            if self.encoder_trainable:
                # [bs, seq_len70, embed_dim768]
                # print('self.local_pred:',self.local_pred.shape)
                # print('self.origin_pred:',self.origin_pred.shape)
                # print('self.local_gradient:',self.local_gradient.shape)

                weights_grad_a = torch.autograd.grad(
                    self.local_pred,
                    self.local_model.encoder_layer.parameters(),
                    grad_outputs=self.local_gradient,
                    retain_graph=True,
                    #allow_unused = True
                )
                for w, g in zip(self.local_model.encoder_layer.parameters(), weights_grad_a):
                    if w.requires_grad:
                        if w.grad != None:
                            w.grad += g.detach()
                        else:
                            w.grad = g.detach()

            weights_grad_a = torch.autograd.grad(
                self.local_pred,
                self.adversarial_model.parameters(),
                grad_outputs=self.local_gradient,
                retain_graph=True,
            )
            for w, g in zip(self.adversarial_model.parameters(), weights_grad_a):
                if w.requires_grad:
                    if w.grad != None:
                        w.grad += g.detach()
                    else:
                        w.grad = g.detach()
            self.local_model_optimizer.step()

        elif (self.args.apply_mid == True and (self.index in self.args.defense_configs["party"])
            and (self.index < self.args.k - 1) and self.mid_position == "out"):
            # print('before')
            # mark = 0
            # for name, param in self.mid_model.named_parameters():
            #     if mark == 0:
            #         print(name, param)
            #         mark = mark + 1

            self.local_model_optimizer.zero_grad()# self.mid_model_optimizer.zero_grad()

            # update mid_model/local_encoder with mid_loss.backward
            self.mid_loss.backward(retain_graph=True)

            # update local encoder with global_loss
            weights_grad_a = torch.autograd.grad(
                self.local_pred,
                self.mid_model.parameters(),
                grad_outputs=self.local_gradient,
                retain_graph=True,
            )
            for w, g in zip(self.mid_model.parameters(), weights_grad_a):
                if w.requires_grad:
                    if w.grad != None:
                        w.grad += g.detach()
                    else:
                        w.grad = g.detach()
        
            if self.encoder_trainable:
                weights_grad_a = torch.autograd.grad(
                    self.local_pred,
                    self.local_model.encoder_layer.parameters(),
                    grad_outputs=self.local_gradient,
                    retain_graph=True,
                )
                for w, g in zip(self.local_model.encoder_layer.parameters(), weights_grad_a):
                    if w.requires_grad:
                        if w.grad != None:
                            w.grad += g.detach()
                        else:
                            w.grad = g.detach()
            
            # print('Backward MID Model:')
            # mark = 0
            # for name, param in self.mid_model.named_parameters():
            #     if mark == 0:
            #         print(name, param)
            #         mark = mark + 1

            # assert 1>2
            self.local_model_optimizer.step()

        
        elif (self.args.apply_mid == True and (self.index in self.args.defense_configs["party"])
            and (self.index < self.args.k - 1) and self.mid_position == "inner"):

            self.local_model_optimizer.zero_grad()# self.mid_model_optimizer.zero_grad()
            
            ###########  update mid_model  ###########
            # with mid_loss.backward
            self.mid_loss.backward(retain_graph=True)
            # with global_loss -> local_gradient
            self.weights_grad_a = torch.autograd.grad(
                self.local_pred,
                self.local_model.inner_mid_model.parameters(),
                grad_outputs=self.local_gradient,
                retain_graph=True,
            )
            for w, g in zip(self.local_model.inner_mid_model.parameters(), self.weights_grad_a):
                if w.requires_grad:
                    if w.grad != None:
                        w.grad += g.detach()
                    else:
                        w.grad = g.detach()
            
            ###########  update local encoder  ###########
            #  with cross_entropy_loss + mid_loss -> local_gradient
            if self.encoder_trainable:
                weights_grad_a = torch.autograd.grad(
                    self.local_pred,
                    self.local_model.encoder_layer.parameters(),
                    grad_outputs=self.local_gradient,
                    retain_graph=True,
                )
                for w, g in zip(self.local_model.encoder_layer.parameters(), weights_grad_a):
                    if w.requires_grad:
                        if w.grad != None:
                            w.grad += g.detach()
                        else:
                            w.grad = g.detach()

            self.local_model_optimizer.step()

            # self.mid_loss = self.local_model.mid_loss
            # print('self.mid_loss:',self.mid_loss)

            # mark = 0
            # for name, param in self.local_model.inner_mid_model.named_parameters():
            #     if mark == 0:
            #         print(name, param.grad)
            #         mark = mark + 1

            # print('after')
            # mark = 0
            # for name, param in self.local_model.inner_mid_model.named_parameters():
            #     if mark == 0:
            #         print(name, param)
            #         mark = mark + 1

        else: # W/O Defense
            if self.local_model_optimizer != None:
                self.local_model_optimizer.zero_grad()

                self.weights_grad_a = torch.autograd.grad(
                    self.local_pred,
                    self.local_model.parameters(),
                    grad_outputs=self.local_gradient,
                    retain_graph=True,
                )
                for w, g in zip(self.local_model.parameters(), self.weights_grad_a):
                    if w.requires_grad:
                        if w.grad != None:
                            w.grad += g.detach()
                        else:
                            w.grad = g.detach()
                
                self.local_model_optimizer.step()


    def calculate_gradient_each_class(self, global_pred, local_pred_list, test=False):
        # print(f"global_pred.shape={global_pred.size()}") # (batch_size, num_classes)
        self.gradient_each_class = [[] for _ in range(global_pred.size(1))]
        one_hot_label = torch.zeros(global_pred.size()).to(global_pred.device)
        for ic in range(global_pred.size(1)):
            one_hot_label *= 0.0
            one_hot_label[:,ic] += 1.0
            if self.train_index != None: # for graph data
                if test == False:
                    loss = self.criterion(global_pred[self.train_index], one_hot_label[self.train_index])
                else:
                    loss = self.criterion(global_pred[self.test_index], one_hot_label[self.test_index])
            else:
                loss = self.criterion(global_pred, one_hot_label)
            for ik in range(self.args.k):
                self.gradient_each_class[ic].append(torch.autograd.grad(loss, local_pred_list[ik], retain_graph=True, create_graph=True))
        # end of calculate_gradient_each_class, return nothing

    def predict(self):
        data_loader_list = [self.test_loader]
        exact_score_list = []
        f1_list = []
        total_sample_cnt = 0
        with torch.no_grad():
            for parties_data in zip(*data_loader_list):
                _parties_data = []
                for party_id in range(len(parties_data)): # iter through each passive party
                    batch_input_ids = []
                    batch_label = []
                    batch_attention_mask = []
                    batch_token_type_ids = []
                    batch_feature = []
                    for bs_id in range(len(parties_data[party_id])):
                        # Input_ids
                        batch_input_ids.append( parties_data[party_id][bs_id][0].tolist() )
                        # Attention Mask
                        batch_attention_mask.append( parties_data[party_id][bs_id][2].tolist()  )

                        # ptoken_type_ids
                        if parties_data[party_id][bs_id][3] == []:
                            batch_token_type_ids = None
                        else:
                            batch_token_type_ids.append( parties_data[party_id][bs_id][3].tolist()  )

                        # feature (for QuestionAnswering only)
                        if parties_data[party_id][bs_id][4] == []:
                            batch_feature = None
                        else:
                            batch_feature.append( parties_data[party_id][bs_id][4] )

                        # Label
                        if type(parties_data[party_id][bs_id][1]) != str:
                            batch_label.append( parties_data[party_id][bs_id][1].tolist()  )
                        else:
                            batch_label.append( parties_data[party_id][bs_id][1]  )

                    batch_input_ids = torch.tensor(batch_input_ids).to(self.device)
                    batch_attention_mask = torch.tensor(batch_attention_mask).to(self.device)
                    if batch_token_type_ids != None:
                        batch_token_type_ids = torch.tensor(batch_token_type_ids).to(self.device)     

                    if type( batch_label[0] ) != str:
                        if self.args.task_type == 'QuestionAnswering':
                            # origin_batch_label_shape [bs, 2, num_of_answers]
                            # 2*bs, num_of_answers
                            batch_label =  pad_sequence([torch.tensor(position_list) for sample_label in batch_label\
                             for position_list in sample_label], batch_first=True, padding_value=-1).to(self.device)
                            
                            origin_batch_label_shape = [int(batch_label.shape[0]/2) , 2, batch_label.shape[1]]
                            batch_label = batch_label.reshape(origin_batch_label_shape)
                            # padded_sequence = pad_sequence([torch.tensor(seq) for sublist in list_of_lists for seq in sublist], batch_first=True, padding_value=0)
                            # print('After batch_label:',batch_label.shape)
                        else:
                            batch_label = torch.tensor(batch_label).to(self.device)
                    

                    _parties_data.append([batch_input_ids,batch_label,batch_attention_mask,batch_token_type_ids,batch_feature] )

                parties_data = _parties_data

                if self.args.task_type == "SequenceClassification" and self.args.num_classes > 1: # classification
                    gt_one_hot_label = self.label_to_one_hot(parties_data[0][1], self.args.num_classes)
                elif self.args.task_type == "QuestionAnswering":
                    gt_one_hot_label = list(parties_data[0][1])
                else:
                    gt_one_hot_label = parties_data[0][1]

                input_shape = parties_data[0][0].shape[:2]  # batchsize, seq_length
                self.input_shape = input_shape
                self.obtain_local_data(parties_data[0][0], parties_data[0][2], parties_data[0][3])
                self.gt_one_hot_label = gt_one_hot_label

                pred_list = self.pred_transmit()
                test_logit = self._send_pred_message(pred_list)

                # generate assessment
                exact_scores, f1s, sample_cnt = self.output(test_logit, gt_one_hot_label, parties_data)
                exact_score_list.extend(exact_scores)
                f1_list.extend(f1s)
                if sample_cnt is not None:
                    total_sample_cnt += sample_cnt
                del parties_data
        
        return exact_score_list, f1_list, total_sample_cnt

    def launch_defense(self, gradients_list, _type):

        if _type == 'gradients':
            return apply_defense(self.args, _type, gradients_list)
        elif _type == 'pred':
            return apply_defense(self.args, _type, gradients_list)
        else:
            # further extention
            return gradients_list

    def apply_defense_on_transmission(self, pred_detach):
        # print('apply_defense_on_transmission')
        # print('pred_detach:',type(pred_detach))
        # print(pred_detach.shape)
        ########### Defense applied on pred transmit ###########
        if self.args.apply_defense == True and self.args.apply_dp == True:
            pred_detach_list = self.launch_defense(pred_detach, "pred")
            pred_detach = torch.stack(pred_detach_list)
            # print('after:',pred_detach.shape)
            
        return pred_detach

    def apply_communication_protocol_on_transmission(self, pred_detach):
        ########### communication_protocols ###########
        if self.args.communication_protocol in ['Quantization','Topk']:
            pred_detach = compress_pred( self.args ,pred_detach , self.parties[ik].local_gradient,\
                            self.current_epoch, self.current_step).to(self.args.device)
        return pred_detach

    def pred_transmit(self):
        if self.args.model_type == 'Bert':
            if self.args.task_type == 'SequenceClassification':
                pred, pred_detach, attention_mask = self.give_pred()  # , _input_shape
            elif self.args.task_type == 'QuestionAnswering':
                pred, pred_detach, attention_mask = self.give_pred()  # , _input_shape
            else:
                assert 1 > 2, "task type not supported for finetune"
        elif self.args.model_type == 'Llama':
            if self.args.task_type == 'SequenceClassification':
                pred, pred_detach, sequence_lengths, attention_mask = self.give_pred()  # , _input_shape
            elif self.args.task_type == 'CausalLM':
                pred, pred_detach, attention_mask  = self.give_pred()  # , _input_shape
            elif self.args.task_type == 'QuestionAnswering':
                pred, pred_detach, attention_mask = self.give_pred()  # , _input_shape
            else:
                assert 1 > 2, "task type not supported for finetune"

        elif self.args.model_type == 'GPT2':
            if self.args.task_type == 'SequenceClassification':
                pred, pred_detach, sequence_lengths, attention_mask = self.give_pred()
            elif self.args.task_type == 'CausalLM':
                pred, pred_detach, attention_mask = self.give_pred()
            elif self.args.task_type == 'QuestionAnswering':
                pred, pred_detach, attention_mask = self.give_pred()
            else:
                assert 1 > 2, "task type not supported for finetune"

        # Defense
        if self.args.apply_defense:
            if (self.index in self.args.defense_configs['party']):
                # print('Apply DP')
                pred_detach = self.apply_defense_on_transmission(pred_detach)
        # Communication Process
        pred_detach = self.apply_communication_protocol_on_transmission(pred_detach)

        pred_clone = torch.autograd.Variable(pred_detach, requires_grad=True).to(self.args.device)
        attention_mask = torch.autograd.Variable(attention_mask).to(self.args.device)

        # receive pred
        if self.args.model_type == 'Bert':
            if self.args.task_type == 'SequenceClassification':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + \
                                           get_size_of(attention_mask)  # MB
            elif self.args.task_type == 'QuestionAnswering':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + get_size_of(attention_mask)  # MB
        elif self.args.model_type == 'Llama':
            if self.args.task_type == 'SequenceClassification':
                pred_list = [pred_clone, sequence_lengths, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + \
                                           get_size_of(sequence_lengths) + \
                                           get_size_of(attention_mask)  # MB
            elif self.args.task_type == 'CausalLM':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + \
                                           get_size_of(attention_mask)  # MB
            elif self.args.task_type == 'QuestionAnswering':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + get_size_of(attention_mask)  # MB
        elif self.args.model_type == 'GPT2':
            if self.args.task_type == 'SequenceClassification':
                pred_list = [pred_clone, sequence_lengths, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + get_size_of(attention_mask) + get_size_of(
                    sequence_lengths)  # MB
            elif self.args.task_type == 'CausalLM':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + get_size_of(attention_mask)  # MB
            elif self.args.task_type == 'QuestionAnswering':
                pred_list = [pred_clone, attention_mask]
                self.update_local_pred(pred_clone)
                self.communication_cost += get_size_of(pred_clone) + get_size_of(attention_mask)  # MB

        return [pred_list]

    def output(self, test_logit, gt_one_hot_label, parties_data):
        test_preds = []
        test_targets = []
        test_predict_labels = []
        test_actual_labels = []
        target_word_list = []
        predict_word_list = []
        suc_cnt = 0
        sample_cnt = 0

        if self.args.task_type == "SequenceClassification":
            if self.args.num_classes == 1:  # regression
                predict_label = test_logit.detach().cpu()
                actual_label = gt_one_hot_label.detach().cpu()

                # full_predict_label = full_pred.detach().cpu()

                predict_label = torch.tensor([_.item() for _ in predict_label])
                actual_label = torch.tensor([_.item() for _ in actual_label])
                
                sample_cnt = predict_label.shape[0]

                return list(predict_label), list(actual_label), sample_cnt
                # test_full_predict_labels.extend( list(full_predict_label) )
            else:  # Classification
                enc_predict_prob = test_logit
                # full_enc_predict_prob = full_pred
                # enc_predict_prob = F.softmax(test_logit, dim=-1)

                predict_label = torch.argmax(enc_predict_prob, dim=-1)
                actual_label = torch.argmax(gt_one_hot_label, dim=-1)
                # full_predict_label = torch.argmax(full_enc_predict_prob, dim=-1)

                test_preds.append(list(enc_predict_prob.detach().cpu().numpy()))
                test_targets.append(list(gt_one_hot_label.detach().cpu().numpy()))
                # full_test_preds.append(list(full_enc_predict_prob.detach().cpu().numpy()))

                # test_full_predict_labels.extend( list(full_predict_label.detach().cpu()) )

                sample_cnt = predict_label.shape[0]
                suc_cnt += torch.sum(predict_label == actual_label).item()
                # full_suc_cnt += torch.sum(full_predict_label == actual_label).item()
                return list(predict_label.detach().cpu()), list(actual_label.detach().cpu()), sample_cnt

        elif self.args.task_type == "CausalLM":
            # get logits of last hidden state
            # print('test_logit:',test_logit.shape) # [batchsize, maxlength512, vocab_size32000]
            next_token_logits = test_logit[:, -1]  # [bs, 32000]

            if self.args.dataset == "Lambada":
                # generated, generate_text = self.parties[self.k-1].generate(pred_list, test="True")
                # print('gt_one_hot_label:',type(gt_one_hot_label),gt_one_hot_label.shape)

                # print('gt_one_hot_label:',type(gt_one_hot_label),gt_one_hot_label) # list of target tokens
                target_word = [normalize_answer(_p) for _p in gt_one_hot_label]  # list of normalized tokens
                # print('target_word:',type(target_word),len(target_word),target_word) #
                target_word_list.extend(target_word)
                # print('target_word_list:',type(target_word_list),len(target_word_list),target_word_list)

                # print('gt_one_hot_label:',type(gt_one_hot_label),gt_one_hot_label)
                # target_word = gt_one_hot_label[0]
                # target_word_list.append(normalize_answer(target_word))
                # print('target_word:',target_word)

                enc_predict_prob = nn.functional.softmax(next_token_logits, dim=-1)

                if self.args.metric_type == "best_pred":
                    predict_label = torch.argmax(enc_predict_prob, dim=-1)  # [bs]
                    predict_word = [self.args.tokenizer.decode([_best_id]) for _best_id in predict_label.tolist()]
                    predict_word = [normalize_answer(_p) for _p in predict_word]
                    predict_word_list.extend(predict_word)  # predict_word: bs * best_pred
                elif self.args.metric_type == "n_best":
                    logit_list, index_list = torch.sort(enc_predict_prob, descending=True)
                    # print('index_list:',index_list.shape)
                    predict_label = index_list[:, :self.args.n_best_size]
                    # print('predict_label:',predict_label.shape)
                    for _bs in range(predict_label.shape[0]):  # each batch
                        predict_word = [self.args.tokenizer.decode([_label]) for _label in predict_label[_bs].tolist()]
                        predict_word = [normalize_answer(_p) for _p in predict_word]
                        predict_word_list.append(predict_word)  # predict_word: list of n best for this batch
                        # print('predict_word:',predict_word)

                return target_word_list, predict_word_list, None

            else:  # MMLU
                choice_id_list = []
                for choice in self.args.label_dict.keys():
                    choice_id_list.append(self.args.tokenizer(choice).input_ids[-1])
                    _id = self.args.tokenizer(choice).input_ids[-1]
                enc = next_token_logits[:, choice_id_list]  # [bs, num_choice]
                enc_predict_prob = nn.functional.softmax(enc, dim=-1)  # [bs, num_choice]

                predict_label = torch.argmax(enc_predict_prob, dim=-1)  # [bs]
                actual_label = gt_one_hot_label  # torch.argmax(gt_one_hot_label, dim=-1)

                test_predict_labels.extend(predict_label.detach().cpu().tolist())
                test_actual_labels.extend(actual_label.detach().cpu().tolist())
                # test_full_predict_labels.extend( list(full_predict_label.detach().cpu()) )

                sample_cnt += predict_label.shape[0]
                suc_cnt += torch.sum(predict_label == actual_label).item()

                return [], [], 0

        elif self.args.task_type == "QuestionAnswering":
            start_logits = test_logit.start_logits
            end_logits = test_logit.end_logits
            sample_cnt = start_logits.shape[0]

            n_best_size = self.args.n_best_size
            start_indexes = [_get_best_indexes(_logits, n_best_size) for _logits in start_logits]
            end_indexes = [_get_best_indexes(_logits, n_best_size) for _logits in end_logits]

            exact_score_list = []
            f1_list = []
            for i in range(start_logits.shape[0]):
                # for each sample in this batch
                _start_logits = start_logits[i]
                _end_logits = end_logits[i]
                _start_indexes = start_indexes[i]
                _end_indexes = end_indexes[i]

                ############ Gold ################
                feature = parties_data[0][4][i]
                # print('parties_data[0][4]:',type(parties_data[0][4]),'feature:',type(feature))
                feature_tokens = [_token[0] for _token in feature["tokens"]]

                gold_start_indexs, gold_end_indexs = gt_one_hot_label[i]  # the i'th sample in a batch
                if len(gold_start_indexs.shape) == 0:
                    gold_start_indexs = gold_start_indexs.unsqueeze(0)
                if len(gold_end_indexs.shape) == 0:
                    gold_end_indexs = gold_end_indexs.unsqueeze(0)

                gold_ans = []  # gold answers for this sample
                for _i in range(len(gold_start_indexs)):
                    gold_start_index = int(gold_start_indexs[_i])
                    gold_end_index = int(gold_end_indexs[_i])
                    if gold_start_index == -1:
                        continue

                    gold_ans_text = " ".join(feature_tokens[gold_start_index:(gold_end_index + 1)])
                    gold_ans_text = normalize_answer(gold_ans_text)
                    gold_ans.append(gold_ans_text)
                # print('gold_ans:',gold_ans,feature["orig_answer_text"])

                ############ Pred ################
                _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
                    "PrelimPrediction",
                    ["start_index", "end_index", "start_logit", "end_logit"])
                _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
                    "NbestPrediction", ["text", "start_logit", "end_logit"])

                # iterate through all possible start-end pairs
                prelim_predictions = []
                for start_index in _start_indexes:
                    for end_index in _end_indexes:
                        # We could hypothetically create invalid predictions, e.g., predict
                        # that the start of the span is in the question. We throw out all
                        # invalid predictions.
                        if start_index >= len(feature["tokens"]):
                            continue
                        if end_index >= len(feature["tokens"]):
                            continue
                        if start_index not in feature["token_to_orig_map"]:
                            continue
                        if end_index not in feature["token_to_orig_map"]:
                            continue
                        if not feature["token_is_max_context"].get(start_index, False):
                            continue
                        if end_index < start_index:
                            continue
                        length = end_index - start_index + 1
                        if length > self.args.max_answer_length:
                            continue

                        prelim_predictions.append(
                            _PrelimPrediction(
                                start_index=start_index,
                                end_index=end_index,
                                start_logit=_start_logits[start_index],
                                end_logit=_end_logits[end_index]))

                # Iterate through Sorted Predictions
                prelim_predictions = sorted(
                    prelim_predictions,
                    key=lambda x: (x.start_logit + x.end_logit),
                    reverse=True)
                exact_score = 0
                f1 = 0
                # Get n best prediction text
                nbest = []
                n_best_size = min(n_best_size, len(prelim_predictions))
                for _id in range(n_best_size):
                    start_index = prelim_predictions[_id].start_index
                    end_index = prelim_predictions[_id].end_index

                    pred_ans_text = " ".join(feature_tokens[start_index:(end_index + 1)])
                    pred_ans_text = normalize_answer(pred_ans_text)

                    nbest.append(
                        _NbestPrediction(
                            text=pred_ans_text,
                            start_logit=prelim_predictions[_id].start_logit,
                            end_logit=prelim_predictions[_id].end_logit))

                # Get best predicted answer
                total_scores = []
                best_non_null_entry = None

                if self.args.metric_type == "best_pred":
                    for entry in nbest:
                        total_scores.append(entry.start_logit + entry.end_logit)
                        if not best_non_null_entry:
                            if entry.text:
                                best_non_null_entry = entry
                    pred_ans_text = best_non_null_entry.text if (best_non_null_entry != None) else ""
                    # Calculate exact_score/f1
                    # print('best pred:',pred_ans_text)
                    exact_score = max(compute_exact(a, pred_ans_text) for a in gold_ans)
                    f1 = max(compute_f1(a, pred_ans_text) for a in gold_ans)
                    # print('this batch:',exact_score,f1)
                    exact_score_list.append(exact_score)
                    f1_list.append(f1)
                elif self.args.metric_type == "n_best":
                    for entry in nbest:
                        total_scores.append(entry.start_logit + entry.end_logit)
                        if not best_non_null_entry:
                            if entry.text:
                                best_non_null_entry = entry
                        pred_ans_text = entry.text  # print('best pred:',pred_ans_text)
                        # Calculate exact_score/f1
                        exact_score = max(exact_score, max(compute_exact(a, pred_ans_text) for a in gold_ans))
                        f1 = max(f1, max(compute_f1(a, pred_ans_text) for a in gold_ans))
                    # print('this batch:',exact_score,f1)
                    exact_score_list.append(exact_score)
                    f1_list.append(f1)
                else:
                    assert 1 > 2, f"{self.args.metric_type} not provided!"

                return exact_score_list, f1_list, sample_cnt #None

        else:
            assert 1 > 2, "task_type not supported"

    def parse_pred_message_result(self, test_logit):
        if self.args.model_type == 'Bert':
            if self.args.task_type == 'SequenceClassification':
                logits = torch.Tensor(test_logit['logits'])
                if test_logit['requires_grad']:
                    logits.requires_grad_()
                    # logits.grad = test_logit['grad_fn']
                return logits.to(self.args.device)
            elif self.args.task_type == 'QuestionAnswering':
                start_logits = torch.Tensor(test_logit['start_logits'])
                end_logits = torch.Tensor(test_logit['end_logits'])
                return QuestionAnsweringModelOutput(
                    loss=None,
                    start_logits=start_logits.to(self.args.device),
                    end_logits=end_logits.to(self.args.device),
                    hidden_states=None,
                    attentions=None,
                )

    def _send_pred_message(self, pred_list):
        return self._communication.send_pred_message(pred_list, self.parse_pred_message_result)

    def _send_global_backward_message(self):
        self._communication.send_global_backward_message()

    def _send_global_loss_and_gradients(self, loss, gradients):
        self._communication.send_global_loss_and_gradients(loss, gradients)

    def _send_cal_passive_local_gradient_message(self, pred):
        self._communication.send_cal_passive_local_gradient_message(pred)

    def _send_global_lr_decay(self, i_epoch):
        self._communication.send_global_lr_decay(i_epoch)

    # def _send_global_model_train_message(self):
    #     self.local_model.train()
    #     self.args.parties[self.args.k - 1].global_model.train()
    
    def _send_global_model_train_message(self):
        self._communication.send_global_model_train_message()

    # def train(self, i_epoch):
    #     data_loader_list = [self.train_loader]
    #     postfix = {'train_loss': 0.0, 'train_acc': 0.0, 'test_acc': 0.0}
    #     i = -1
    #     print_every = 1
    #     total_time = 0

    #     for parties_data in zip(*data_loader_list):
    #         ############ Allocate Data #################
    #         # parties_data[0]:  bs *( data, label, mask, token_type_ids, feature(forQA))
    #         _parties_data = []
    #         for party_id in range(len(parties_data)):  # iter through each passive party
    #             batch_input_ids = []
    #             batch_label = []
    #             batch_attention_mask = []
    #             batch_token_type_ids = []
    #             batch_feature = []
    #             for bs_id in range(len(parties_data[party_id])):
    #                 # Input_ids
    #                 batch_input_ids.append(parties_data[party_id][bs_id][0].tolist())
    #                 # Attention Mask
    #                 batch_attention_mask.append(parties_data[party_id][bs_id][2].tolist())

    #                 # ptoken_type_ids
    #                 if parties_data[party_id][bs_id][3] == []:
    #                     batch_token_type_ids = None
    #                 else:
    #                     batch_token_type_ids.append(parties_data[party_id][bs_id][3].tolist())

    #                 # feature (for QuestionAnswering only)
    #                 if parties_data[party_id][bs_id][4] == []:
    #                     batch_feature = None
    #                 else:
    #                     batch_feature.append(parties_data[party_id][bs_id][4])

    #                 # Label
    #                 if type(parties_data[party_id][bs_id][1]) != str:
    #                     batch_label.append(parties_data[party_id][bs_id][1].tolist())
    #                 else:
    #                     batch_label.append(parties_data[party_id][bs_id][1])

    #             batch_input_ids = torch.tensor(batch_input_ids).to(self.device)
    #             batch_attention_mask = torch.tensor(batch_attention_mask).to(self.device)
    #             if batch_token_type_ids != None:
    #                 batch_token_type_ids = torch.tensor(batch_token_type_ids).to(self.device)
    #             if type(batch_label[0]) != str:
    #                 batch_label = torch.tensor(batch_label).to(self.device)

    #             _parties_data.append(
    #                 [batch_input_ids, batch_label, batch_attention_mask, batch_token_type_ids, batch_feature])

    #         parties_data = _parties_data

    #         if self.args.task_type == "SequenceClassification" and self.args.num_classes > 1: # classification
    #             gt_one_hot_label = self.label_to_one_hot(parties_data[self.index][1], self.args.num_classes)
    #         else:
    #             gt_one_hot_label = parties_data[self.index][1]

    #         i += 1
    #         self._send_global_model_train_message() # call global model to a training mode

    #         # ====== train batch (start) ======
    #         enter_time = time.time()
    #         self.loss, self.train_acc = self.train_batch(parties_data, gt_one_hot_label)
    #         exit_time = time.time()
    #         total_time += (exit_time - enter_time)
    #         self.num_total_comms = self.num_total_comms + 1
    #         # if self.num_total_comms % 10 == 0:
    #         #     print(f"total time for {self.num_total_comms} communication is {total_time}")

    #         # if self.train_acc > STOPPING_ACC[str(self.args.dataset)] and flag == 0:
    #         #     self.stopping_time = total_time
    #         #     self.stopping_iter = self.num_total_comms
    #         #     self.stopping_commu_cost = self.communication_cost
    #         #     flag = 1
    #         # ====== train batch (end) ======

    #         self.current_step = self.current_step + 1

    #         # del(self.parties_data) # remove from cuda
    #         # del(parties_data)

    #     # if self.args.apply_attack == True:
    #     #     if (self.args.attack_name in LABEL_INFERENCE_LIST) and i_epoch==1:
    #     #         print('Launch Label Inference Attack, Only train 1 epoch')
    #     #         break

    #     # self.trained_models = self.save_state(True)
    #     # if self.args.save_model == True:
    #     #     self.save_trained_models()

    #     # LR decay
    #     self._send_global_lr_decay(i_epoch)
    #     # LR record
    #     # if self.args.k == 2:
    #     #     LR_passive_list.append(self.parties[0].give_current_lr())
    #         # LR_active_list.append(self.parties[1].give_current_lr())
    #     return self.loss, self.train_acc

    
    # def train_batch(self, parties_data, batch_label):
    #     '''
    #     batch_label: self.gt_one_hot_label   may be noisy
    #         QA: bs * [start_position, end_position]
    #     '''
    #     ############### allocate data ###############
    #     # encoder = self.args.encoder
    #     # if self.args.apply_cae:
    #     #     assert encoder != None, "[error] encoder is None for CAE"
    #     #     _, gt_one_hot_label = encoder(batch_label)
    #     # else:
    #     gt_one_hot_label = batch_label

    #     # allocate data (data/label/attention_mask/token_type_ids)
    #     input_shape = parties_data[0][0].shape[:2]  # parties_data[ik][0].size()
    #     self.input_shape = input_shape
    #     self.obtain_local_data(parties_data[0][0], parties_data[0][2], parties_data[0][3])
    #     self.gt_one_hot_label = gt_one_hot_label
    #     ############### allocate data ###############

    #     ################ normal vertical federated learning ################
    #     torch.autograd.set_detect_anomaly(True)

    #     # =================== Commu ===================
    #     # exchange info between party: local_pred/global_pred
    #     all_pred_list = self.pred_transmit()   # [ pred of this party ]
    #     final_pred = self._send_pred_message(all_pred_list) # TODO: if there's multiple more passive parties
    #     # =================== Commu ===================

    #     # passive party -> global gradient -> active party
    #     self.global_gradient_transmit(final_pred)

    #     # active party -> local gradient -> passive party
    #     if self.local_model_optimizer != None:
    #         self.local_gradient_transmit(all_pred_list)

    #     # ============= Model Update =============
    #     self._send_global_backward_message()  # update parameters for global trainable part
    #     # if self.parties[ik].local_model_optimizer != None:
    #     self.local_backward()
    #     # ============= Model Update =============

    #     ################ normal vertical federated learning ################

    #     # print train_acc each batch
    #     if self.args.task_type == 'QuestionAnswering':
    #         pred = self.args.parties[self.args.k - 1].global_pred  # QuestionAnsweringModelOutput
    #         loss = self.args.parties[self.args.k - 1].global_loss

    #         start_logits = pred.start_logits
    #         end_logits = pred.end_logits

    #         n_best_size = self.args.n_best_size
    #         start_indexes = [_get_best_indexes(_logits, n_best_size) for _logits in start_logits]
    #         end_indexes = [_get_best_indexes(_logits, n_best_size) for _logits in end_logits]

    #         exact_score_list = []
    #         f1_list = []
    #         # for each sample in this batch
    #         for i in range(start_logits.shape[0]):
    #             _start_logits = start_logits[i]
    #             _end_logits = end_logits[i]
    #             _start_indexes = start_indexes[i]
    #             _end_indexes = end_indexes[i]

    #             ############ Gold ################
    #             feature = parties_data[0][4][i]
    #             feature_tokens = [_token[0] for _token in feature["tokens"]]

    #             gold_start_indexs, gold_end_indexs = gt_one_hot_label[i]  # the i'th sample in a batch
    #             if len(gold_start_indexs.shape) == 0:
    #                 gold_start_indexs = gold_start_indexs.unsqueeze(0)
    #             if len(gold_end_indexs.shape) == 0:
    #                 gold_end_indexs = gold_end_indexs.unsqueeze(0)
    #             gold_ans = []  # gold answers for this sample
    #             for _i in range(len(gold_start_indexs)):
    #                 gold_start_index = int(gold_start_indexs[_i])
    #                 gold_end_index = int(gold_end_indexs[_i])
    #                 gold_ans_text = " ".join(feature_tokens[gold_start_index:(gold_end_index + 1)])
    #                 gold_ans_text = normalize_answer(gold_ans_text)
    #                 gold_ans.append(gold_ans_text)
    #             # print('gold_ans:',gold_ans,feature["orig_answer_text"])

    #             ############ Pred ################
    #             _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
    #                 "PrelimPrediction",
    #                 ["start_index", "end_index", "start_logit", "end_logit"])
    #             _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
    #                 "NbestPrediction", ["text", "start_logit", "end_logit"])

    #             # iterate through all possible start-end pairs
    #             prelim_predictions = []
    #             for start_index in _start_indexes:
    #                 for end_index in _end_indexes:
    #                     # We could hypothetically create invalid predictions, e.g., predict
    #                     # that the start of the span is in the question. We throw out all
    #                     # invalid predictions.
    #                     if start_index >= len(feature["tokens"]):
    #                         continue
    #                     if end_index >= len(feature["tokens"]):
    #                         continue
    #                     if start_index not in feature["token_to_orig_map"]:
    #                         continue
    #                     if end_index not in feature["token_to_orig_map"]:
    #                         continue
    #                     if not feature["token_is_max_context"].get(start_index, False):
    #                         continue
    #                     if end_index < start_index:
    #                         continue
    #                     length = end_index - start_index + 1
    #                     if length > self.args.max_answer_length:
    #                         continue

    #                     prelim_predictions.append(
    #                         _PrelimPrediction(
    #                             start_index=start_index,
    #                             end_index=end_index,
    #                             start_logit=_start_logits[start_index],
    #                             end_logit=_end_logits[end_index]))

    #             # Iterate through Sorted Predictions
    #             prelim_predictions = sorted(
    #                 prelim_predictions,
    #                 key=lambda x: (x.start_logit + x.end_logit),
    #                 reverse=True)
    #             exact_score = 0
    #             f1 = 0
    #             # Get n best prediction text
    #             nbest = []
    #             n_best_size = min(n_best_size, len(prelim_predictions))
    #             for _id in range(n_best_size):
    #                 start_index = prelim_predictions[_id].start_index
    #                 end_index = prelim_predictions[_id].end_index

    #                 pred_ans_text = " ".join(feature_tokens[start_index:(end_index + 1)])
    #                 pred_ans_text = normalize_answer(pred_ans_text)

    #                 nbest.append(
    #                     _NbestPrediction(
    #                         text=pred_ans_text,
    #                         start_logit=prelim_predictions[_id].start_logit,
    #                         end_logit=prelim_predictions[_id].end_logit))

    #             # Get best predicted answer
    #             total_scores = []
    #             best_non_null_entry = None

    #             if self.args.metric_type == "best_pred":
    #                 for entry in nbest:
    #                     total_scores.append(entry.start_logit + entry.end_logit)
    #                     if not best_non_null_entry:
    #                         if entry.text:
    #                             best_non_null_entry = entry
    #                 pred_ans_text = best_non_null_entry.text if (best_non_null_entry != None) else ""
    #                 # Calculate exact_score/f1 for best pred
    #                 # print('best pred:',pred_ans_text)
    #                 exact_score = max(compute_exact(a, pred_ans_text) for a in gold_ans)
    #                 f1 = max(compute_f1(a, pred_ans_text) for a in gold_ans)
    #                 # print('this batch:',exact_score,f1)
    #                 exact_score_list.append(exact_score)
    #                 f1_list.append(f1)
    #             elif self.args.metric_type == "n_best":
    #                 for entry in nbest:
    #                     total_scores.append(entry.start_logit + entry.end_logit)
    #                     if not best_non_null_entry:
    #                         if entry.text:
    #                             best_non_null_entry = entry
    #                     pred_ans_text = entry.text  # print('best pred:',pred_ans_text)
    #                     # Calculate best exact_score/f1 among n best preds
    #                     exact_score = max(exact_score, max(compute_exact(a, pred_ans_text) for a in gold_ans))
    #                     f1 = max(f1, max(compute_f1(a, pred_ans_text) for a in gold_ans))
    #                 # print('this batch:',exact_score,f1)
    #                 exact_score_list.append(exact_score)
    #                 f1_list.append(f1)
    #             else:
    #                 assert 1 > 2, f"{self.args.metric_type} not provided!"

    #         exact_score = np.mean(exact_score_list)
    #         f1 = np.mean(f1_list)

    #         return loss.item(), exact_score

    #     elif self.args.task_type == 'SequenceClassification':
    #         # ###### Noisy Label Attack #######
    #         # convert back to clean label to get true acc
    #         if 'apply_nl' in self.args and self.args.apply_nl:
    #             real_batch_label = self.clean_one_hot_label
    #         else:
    #             real_batch_label = batch_label
    #         # ###### Noisy Label Attack #######

    #         pred = final_pred
    #         loss = self.global_loss
    #         predict_prob = F.softmax(pred, dim=-1)
    #         # if self.args.apply_cae:
    #         #     predict_prob = encoder.decode(predict_prob)

    #         suc_cnt = torch.sum(torch.argmax(predict_prob, dim=-1) == torch.argmax(real_batch_label, dim=-1)).item()
    #         batch_train_acc = suc_cnt / predict_prob.shape[0]

    #         return loss.item(), batch_train_acc

    #     elif self.args.task_type == 'CausalLM':
    #         pred = self.parties[self.k - 1].global_pred  # logits
    #         loss = self.parties[self.k - 1].global_loss
    #         test_logit = pred
    #         next_token_logits = test_logit[:,
    #                             -1]  # [bs, 32000] # print('next_token_logits:',next_token_logits.shape,next_token_logits)

    #         if self.args.dataset == "Lambada":
    #             # print('gt_one_hot_label:',type(gt_one_hot_label),gt_one_hot_label)
    #             target_word_list = [normalize_answer(_p) for _p in gt_one_hot_label]
    #             # print('target_word_list:',type(target_word_list),len(target_word_list),target_word_list)

    #             # predict_word_list : bs * predicted words
    #             enc_predict_prob = nn.functional.softmax(next_token_logits, dim=-1)
    #             if self.args.metric_type == "best_pred":
    #                 predict_label = torch.argmax(enc_predict_prob, dim=-1)  # [bs]
    #                 predict_word = [self.args.tokenizer.decode([_best_id]) for _best_id in predict_label.tolist()]
    #                 predict_word_list = [normalize_answer(_p) for _p in predict_word]
    #             elif self.args.metric_type == "n_best":
    #                 logit_list, index_list = torch.sort(enc_predict_prob, descending=True)
    #                 # print('index_list:',index_list.shape)
    #                 predict_label = index_list[:, :self.args.n_best_size]
    #                 # print('predict_label:',predict_label.shape)
    #                 predict_word_list = []
    #                 for _bs in range(predict_label.shape[0]):  # each batch
    #                     predict_word = [self.args.tokenizer.decode([_label]) for _label in predict_label[_bs].tolist()]
    #                     predict_word = [normalize_answer(_p) for _p in predict_word]
    #                     predict_word_list.append(predict_word)  # predict_word: list of n best for this batch
    #                     # print('predict_word:',predict_word)
    #             # print('predict_word_list:',type(predict_word_list),len(predict_word_list))
    #             # print(predict_word_list)

    #             if self.args.metric_type == "best_pred":
    #                 suc_cnt = 0
    #                 for i in range(len(target_word_list)):
    #                     if target_word_list[i] == predict_word_list[i]:
    #                         suc_cnt += 1
    #                 batch_train_acc = suc_cnt / float(len(target_word_list))  # ACC
    #             elif self.args.metric_type == "n_best":
    #                 suc_cnt = 0
    #                 for i in range(len(target_word_list)):
    #                     if target_word_list[i] in predict_word_list[i]:
    #                         suc_cnt += 1
    #                 batch_train_acc = suc_cnt / float(len(target_word_list))  # ACC
    #             else:
    #                 assert 1 > 2, 'metric type not supported'


    #         else:  # MMLU
    #             choice_id_list = []
    #             for choice in self.args.label_dict.keys():
    #                 choice_id_list.append(self.args.tokenizer(choice).input_ids[-1])
    #                 _id = self.args.tokenizer(choice).input_ids[-1]
    #             enc = next_token_logits[:, choice_id_list]  # [bs, num_choice]
    #             enc_predict_prob = nn.functional.softmax(enc, dim=-1)  # [bs, num_choice]

    #             predict_label = torch.argmax(enc_predict_prob, dim=-1)  # [bs]
    #             actual_label = gt_one_hot_label  # torch.argmax(gt_one_hot_label, dim=-1)

    #             test_predict_labels.extend(predict_label.detach().cpu().tolist())
    #             test_actual_labels.extend(actual_label.detach().cpu().tolist())
    #             # test_full_predict_labels.extend( list(full_predict_label.detach().cpu()) )

    #             sample_cnt += predict_label.shape[0]
    #             suc_cnt += torch.sum(predict_label == actual_label).item()

    #         return loss.item(), batch_train_acc

    def local_gradient_transmit(self, pred):
        if self.local_model_optimizer != None:
            passive_local_gradient= self._send_cal_passive_local_gradient_message(pred)
            self.local_gradient = passive_local_gradient
        print('Passive Party receive self.local_gradient(passive_local_gradient)')
        print(self.local_gradient)

    def global_gradient_transmit(self, pred_list):
        global_loss = self.cal_loss(pred_list)  # raw global loss

        global_gradients = self.cal_global_gradient(global_loss, pred_list)
        self.communication_cost += get_size_of(global_gradients)

        self._send_global_loss_and_gradients(self.global_loss, self.global_gradients)



# class PassiveParty_LLM(Party_LLM):
#     def __init__(self, args, index):
#         super().__init__(args, index)

#     def prepare_data(self, args, index):
#         super().prepare_data(args, index)
#         self.train_dst = PassiveDataset_LLM(args, self.train_data)

#         print('Passive self.train_dst:',len(self.train_dst), type(self.train_dst[0]), type(self.train_dst[1]) )

#         self.test_dst = PassiveDataset_LLM(args,self.test_data)

#         # self.train_dst = PassiveDataset(self.train_data)
#         # self.test_dst = PassiveDataset(self.test_data)

#         # if self.args.need_auxiliary == 1:
#         #     self.aux_dst = ActiveDataset(self.aux_data, self.aux_label)
#             # self.aux_loader = DataLoader(self.aux_dst, batch_size=batch_size,shuffle=True)