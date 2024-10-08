import torch
import random
import copy
from student import aux_student
import numpy as np
from train_utils import evaluate_model
from metrics import Metric
import pdb
from scipy.stats import entropy
from torch.nn import Softmax

softmax = Softmax()

class handler_LLM:
    def __init__(self, args, student, task):
        self.budget_arr = [int(elem) for elem in args.budget.split(",")]
        self.cost = args.cost_ext
        self.budget_models = []
        self.thresholds = []
        self.active = None
        self.task = task
        self.args = args
        self.checkpoint = args.checkpoint
        if task.is_classification:
            self.dic_classes = list(task.classes_dict_gold.values())
        self.target = args.target
        self.student = student
        self.student_vec = [copy.deepcopy(student.model).cpu()]
        self.cache = {}
        self.strat = args.strategy
        self.hparam = args.p_strat
        self.soft_labels = args.soft_labels
        self.n_online = 0
        self.ignore_llm = args.ignore_llm
        self.n_init = args.n_init
        self.missing = len(task.data["online_dataloader"]) - self.n_init
        self.rate = self.budget_arr[-1] / (self.missing)
        self.BT = []
        self.MV = []
        self.EN = []
        # Stores the similarity values when the strategy is CS
        self.CS_similarities = []
        self.output = None
        self.embeds = None
        self.oracle = args.oracle
        self.oracle_BT = args.oracle_BT
        self.labels_embeds = {}
        self.retrain = False
        self.update = False
        self.steps = []
        # This could be a parameter, determines the number of data-points considered when using a dynamic threshold.
        self.window_size_threshold = 50
        if self.strat == "CS":
            self.encoder = copy.deepcopy(self.student.model.model).cpu()

    def oracle_check(self, input):
        tgt = torch.flatten(input.llm_hard).tolist()
        for idx, element in enumerate(torch.flatten(input.gold_hard).tolist()):
            if element != tgt[idx]:
                return False
        return True

    def oracle_check_BT(self, input):
        aux = input.llm_soft[0].sort().values.tolist()
        BT = abs(aux[-1] - aux[-2])
        if BT > self.oracle_BT:
            return True
        return False

    def call_llm(self, input):
        if self.target == "gold":
            if self.soft_labels:
                return input["gold_soft"]
            return input["gold_hard"]
        if self.soft_labels:
            return input["llm_soft"]
        return input["llm_hard"]

    def retrieve_cache(self):
        return self.cache

    def delete_cache(self):
        del self.cache
        return

    def save_cache(self, input, step):
        '''
        Cache here contains the data that the student model will be trained on. 
        '''
        if self.oracle and not self.oracle_check(input):
            return
        aux = copy.deepcopy(torch.flatten(input.llm_soft).tolist())
        aux.sort()
        aux = aux[-1] - aux[-2]
        if self.ignore_llm > 0 and aux <= self.ignore_llm:
            return
        if not "input_ids" in self.cache:
            self.cache["input_ids"] = [torch.flatten(input.input_ids).tolist()]
            self.cache["gold_hard"] = [torch.flatten(input.gold_hard).tolist()]
            if self.task.is_classification:
                self.cache["gold_soft"] = [torch.flatten(input.gold_soft).tolist()]
                self.cache["llm_soft"] = [torch.flatten(input.llm_soft).tolist()]
            self.cache["llm_hard"] = [torch.flatten(input.llm_hard).tolist()]
            return
        self.cache["input_ids"].append(torch.flatten(input.input_ids).tolist())
        self.cache["gold_hard"].append(torch.flatten(input.gold_hard).tolist())
        if self.task.is_classification:
            self.cache["gold_soft"].append(torch.flatten(input.gold_soft).tolist())
            self.cache["llm_soft"].append(torch.flatten(input.llm_soft).tolist())
        self.cache["llm_hard"].append(torch.flatten(input.llm_hard).tolist())
        self.steps.append(step)

    def reset_buffer(self):
        '''
        If cache contains more items than the retrain frequency, 
        remove the older ones so that its length is at most equal to the retrain frequency.
        '''
        keys_to_trim = ["llm_soft", "llm_hard", "input_ids", "gold_hard", "gold_soft"]
        
        # Check the length of the lists in the keys that are present in the data
        list_lengths = {key: len(self.cache[key]) for key in keys_to_trim if key in self.cache}
        
        if not list_lengths:
            return self.cache  # No lists to trim
        
        # Ensure all lists are of the same length
        if len(set(list_lengths.values())) != 1:
            raise ValueError("All lists must have the same length.")
        
        # If the lists have more than retrain_freq items, trim them to the last retrain_freq items
        if list_lengths[next(iter(list_lengths))] > self.args.retrain_freq:
            print('Trimming cache')
            for key in list_lengths.keys():
                self.cache[key] = self.cache[key][-500:]
        
        return self.cache

    def decide(self, input):
        '''
        Decide whether to use budget for LLM annotation.
        '''
        if self.oracle and not self.oracle_check(input):
            return False
        if self.oracle_BT and not self.oracle_check_BT(input):
            return False
        self.budget_arr = [b - self.cost for b in self.budget_arr]
        self.retrain = False
        for b in self.budget_arr:
            if b == 0:
                self.retrain = True
        if self.budget_arr[-1] >= 0:
            if (
                (self.n_online <= self.n_init and self.checkpoint == "-1")
                or (self.missing * self.cost <= self.budget_arr[-1])
                or (self.strat == "MV" and self.n_init == 100 and self.n_online <= 400)
            ):
                if self.strat == "CS":
                    self.obtain_embed(input)
                return True
            if not self.active is None:
                tmp = self.n_online - 1
                if tmp in self.active:
                    return True
                self.retrain = False
                self.budget_arr = [b + self.cost for b in self.budget_arr]
                return False
            if self.strat == "b1":
                return True
            if self.strat == "b2" and random.random() > (1 - self.rate):
                return True
            if self.strat == "EN":
                if (self.args.dynamic_threshold == 1) and (len(self.EN) > self.window_size_threshold):
                    if self.is_outlier(self.EN, 'gt'):
                        return True
                elif self.EN[-1] > self.hparam:
                    return True
            if self.strat == "BT":
                if (self.args.dynamic_threshold == 1) and (len(self.BT) > self.window_size_threshold):
                    if self.is_outlier(self.BT, 'lt'):
                        return True
                elif self.BT[-1] < self.hparam:
                    return True
            if self.strat == "MV":
                # here 5 6
                if len(self.student_vec) < 5 or self.make_assembly(input):
                    return True
            if self.strat == "CS":
                self.obtain_embed(input)
                if (self.args.dynamic_threshold == 1) and (len(self.CS_similarities) > self.window_size_threshold):
                    if self.is_outlier(self.CS_similarities, 'gt'):
                        return True
                elif self.CS_similarities[-1] < self.hparam:
                    return True
        self.retrain = False
        self.budget_arr = [b + self.cost for b in self.budget_arr]
        return False

    def retrieve_candidate(self):
        # find the candidate
        aux = torch.matmul(self.embed, self.embeds.T)
        sorted, indices = torch.sort(aux)
        return self.labels_embeds[indices[0][-1].tolist()], sorted[0][-1]

    def obtain_embed(self, input):
        with torch.no_grad():
            aux_output = self.encoder.encoder(
                input_ids=input.input_ids.cpu(),
                attention_mask=input.attention_mask.cpu(),
                return_dict=True,
            )
            pooled_sentence = (
                aux_output.last_hidden_state
            )  # shape is [batch_size, seq_len, hidden_size]
            self.embed = torch.mean(pooled_sentence, dim=1)
            self.embed = self.embed / torch.norm(self.embed)
            self.embed = self.embed.cpu()
        return

    def save_embed(self):
        if self.embeds is None:
            self.embeds = self.embed
            self.labels_embeds[0] = self.output
            return
        self.embeds = torch.cat((self.embeds, self.embed))
        self.labels_embeds[len(list(self.labels_embeds.keys()))] = self.output
        return

    def query(self, input, step):
        '''
        Returns two values (decision, prediction)
        * decision: if LLM used 1, otherwise 0
        * prediction:  
        Also sets wrap.performance which is a 3-digit binary number. Digit:
        * 1: the decision
        * 2: student acc
        * 3: llm acc 
        '''
        self.n_online += 1
        self.missing -= 1
        new_budgets = len(self.budget_arr) - len(self.budget_models)
        old_budgets = len(self.budget_models)

        self.output = self.student.query(input)

        self.calculate_acc(input)

        previous_outputs = []
        for budget_model in self.budget_models:
            aux = aux_student(budget_model, self.student.args, self.task)
            previous_outputs.append(aux.query(input))

        # MS distance average
        aux = self.output[0].sort().values.tolist()
        self.BT.append(abs(aux[-1] - aux[-2]))
        self.EN.append(abs(entropy(softmax(self.output[0][:100]))))
        if self.args.strategy == 'CS':
            candidate, similarity = self.retrieve_candidate()
            self.CS_similarities.append(similarity)

        if self.decide(input):
            self.output = self.call_llm(input)
            self.save_cache(input, step)
            self.performance = "1" + str(self.st_acc) + str(self.llm_acc)
            if self.strat == "CS":
                self.save_embed()
            return old_budgets * [0] + new_budgets * [
                1
            ], previous_outputs + new_budgets * [self.output]
        self.performance = "0" + str(self.st_acc) + str(self.llm_acc)
        return len(self.budget_arr) * [0], previous_outputs + new_budgets * [self.output]

    def calculate_acc(self, input):
        self.st_acc = int(
            1
            * (self.output.copy()[0].argsort()[-1] == input.gold_soft.argsort()[0][-1])
        )
        self.llm_acc = int(
            1
            * (
                self.call_llm(input)[0].argsort()[-1]
                == input.gold_soft.argsort()[0][-1]
            )
        )

    def make_assembly(self, input):
        target = self.output[0].argmax()
        votes = 0
        for idx in range(len(self.student_vec) - 1):
            tmp_st = aux_student(self.student_vec[idx], self.student.args, self.task)
            output_aux = tmp_st.query(input)
            if output_aux[0].argmax() == target:
                votes += 1
        del tmp_st
        # we can have at maximum 4 votes
        # n_votes=4 is b1
        # we need to check 2, 3
        if votes <= int(self.hparam):
            return True
        return False

    def reorder_students(self):
        # First case: we do MV, we don't support multiple budgets
        if self.strat == "MV":
            # here 5 6
            if len(self.student_vec) == 5:
                for idx in range(4):
                    self.student_vec[idx] = copy.deepcopy(
                        self.student_vec[idx + 1]
                    ).cpu()
                self.student_vec[-1] = copy.deepcopy(self.student.model).cpu()
            else:
                self.student_vec.append(copy.deepcopy(self.student.model).cpu())
            return
        # Second case: we don't do MV, we have multiple budgets
        # We have expired the budget of some method
        if self.retrain and self.strat != "MV":
            self.budget_models.append(copy.deepcopy(self.student.model).cpu())
            # We need to change the student model back to what it was before
            if self.budget_arr[-1] > 0:
                self.student.model = copy.deepcopy(self.student_vec[-1]).cuda()
            self.retrain = False
            return
        return

    def is_outlier(self, data, comparison):
        '''
        Determines whether the latest data-point is an outlier based on how much
        its selection policy value deviates from the previous ones.
        '''
        # set to optimal params depending on the strategy
        dyn_thr = 0.9 if self.args.strategy == 'CS' else 0.7

        latest_value = data[-1]
        previous_data = data[-self.window_size_threshold:-1]

        mean = np.mean(previous_data)
        std_dev = np.std(previous_data)
        
        latest_value = data[-1]
        if comparison == 'gt':
            threshold = (mean + std_dev) * dyn_thr
            is_outlier = latest_value > threshold
        else:
            threshold = (mean - std_dev) * dyn_thr
            if threshold < 0:
                return False
            is_outlier = latest_value < threshold
        self.thresholds.append(threshold)
        return is_outlier