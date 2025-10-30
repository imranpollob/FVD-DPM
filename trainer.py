import os
import time
from tqdm import tqdm, trange
import numpy as np
import torch
from utils.loader import load_seed, load_data, load_model_params, load_model_optimizer, \
                         load_batch, load_loss_fn
from utils.logger import Logger, set_log, start_log, train_log
from sklearn.metrics import roc_auc_score


def p_r_f1(labels, preds):
    TP, FP, TN, FN = 0, 0, 0, 0
    for i in range(len(labels)):
        if labels[i] == 1 and preds[i] == 1:
            TP += 1
        if labels[i] == 0 and preds[i] == 1:
            FP += 1
        if labels[i] == 1 and preds[i] == 0:
            FN += 1
        if labels[i] == 0 and preds[i] == 0:
            TN += 1

    precision = (TP + 1) / (TP + FP + 1)
    recall = (TP + 1) / (TP + FN + 1)
    f1 = 2 / (1 / precision + 1 / recall)
    MCC = (TP * TN - FP * FN) / ((TP + FP) * (TP + FN) * (TN + FP) * (TN + FN) + 1e-7) ** 0.5
    return recall, f1, MCC


def get_accuracy(updated_y, y, node_lines, arg):
    node_lines = node_lines.view(-1, arg.block_size)
    node_lines = node_lines.cpu().detach().numpy().tolist()
    y = torch.argmax(y, dim=-1)
    y_ = y.view(-1, arg.block_size)
    y_ = y_.cpu().detach().numpy().tolist()

    updated_y = torch.argmax(updated_y, dim=-1)
    labels = y.cpu().detach().numpy()
    preds = updated_y.cpu().detach().numpy()
    recall, f1, MCC = p_r_f1(labels, preds)
    auc = roc_auc_score(labels, preds)

    preds_ = updated_y.view(-1, arg.block_size)
    preds_ = preds_.cpu().detach().numpy().tolist()

    IOUs = []
    for i, pred in enumerate(preds_):
        u, v = [], []
        node_line = node_lines[i]
        for j, pre in enumerate(pred):
            if pre == 1 and node_line[j] != -1:
                u.append(node_line[j])
        for j, tru in enumerate(y_[i]):
            if tru == 1 and node_line[j] != -1:
                v.append(node_line[j])
        u, v = set(u), set(v)
        inter, uni = u.intersection(v), u.union(v)
        if len(uni) != 0:
            IOU = len(inter) / len(uni)
            IOUs.append(IOU)
    avg_IOU = np.mean(IOUs)

    return f1, recall, auc, MCC, avg_IOU


def prepare_training_dataset(train_loader, time_batch, device):
    x, adj, y, batch, line = load_batch(train_loader, device)
    x_list, adj_list = [], []
    for i in range(0, time_batch):
        x_list.append(x)
        adj_list.append(adj + x.shape[0] * i)
    x = torch.cat(x_list, dim=0)
    adj = torch.cat(adj_list, dim=1)
    return x, adj, y, batch, line


class Trainer(object):
    def __init__(self, config):
        super(Trainer, self).__init__()

        self.config = config
        self.log_folder_name, self.log_dir = set_log(self.config)
        self.seed = load_seed(self.config.seed)
        self.device = torch.device('cuda', torch.distributed.get_rank())
        self.train_loader, self.valid_loader, self.test_loader = load_data(self.config)
        self.losses = load_loss_fn(self.config, self.device)

    def train(self, ts):
        self.config.exp_name = ts
        self.ckpt = f'{ts}'
        print('\033[91m' + f'{self.ckpt}' + '\033[0m')

        self.params = load_model_params(self.config)
        self.model, self.optimizer, self.scheduler = load_model_optimizer(self.params, self.config.train, self.device)
        logger = Logger(str(os.path.join(self.log_dir, f'{self.config.dataset}_{self.ckpt}.log')), mode='a')
        logger.log(f'{self.ckpt}', verbose=False)
        start_log(logger, self.config)
        train_log(logger, self.config)
        self.loss_fn = self.losses.loss_fn
        self.evaluator = self.losses.test

        best_f1, best_iou = 0.0, 0.0

        for epoch in range(0, self.config.train.num_epochs):
            tr_num, train_loss = 0, 0
            for step, batch_loader in enumerate(self.train_loader):
                x, adj, y, batch, line = prepare_training_dataset(batch_loader, self.config.train.time_batch, self.device)
                loss_subject = (x, adj, y)
                t_start = time.time()
                self.model.train()
                self.optimizer.zero_grad()
                loss = self.loss_fn(self.model, *loss_subject)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.train.grad_norm)
                self.optimizer.step()
                if self.config.train.lr_schedule:
                    self.scheduler.step()
                tr_num += 1
                train_loss += loss.item()
            train_loss = round(train_loss / tr_num, 5)

            if epoch % self.config.train.print_interval == 0:
                tqdm.write(f"[EPOCH {epoch + 1:04d}] | train_loss: {train_loss}")

        # ✅ Always save model after training (Fix Option 2)
        output_dir = os.path.join(self.config.model.output_dir, self.config.dataset)
        os.makedirs(output_dir, exist_ok=True)
        save_path = os.path.join(output_dir, "model_final.bin")
        if torch.distributed.get_rank() == 0:
            torch.save(self.model, save_path)
            print(f"✅ Final model saved to {save_path}")

    def test(self, ts):
        self.evaluator = self.losses.test
        self.params = load_model_params(self.config)
        self.model, self.optimizer, self.scheduler = load_model_optimizer(self.params, self.config.train, self.device)

        output_dir = os.path.join(self.config.model.output_dir, self.config.dataset)
        output_dir = os.path.join(output_dir, "model_final.bin")  # load the final model
        self.model = torch.load(output_dir)
        self.model.eval()

        y_pred, y_truth, node_lines = [], [], []
        for _, test_block in enumerate(self.test_loader):
            x_test, adj_test, y_test, _, line_test = load_batch(test_block, self.device)
            with torch.no_grad():
                _, updated_y, y = self.evaluator(self.model, x_test, adj_test, y_test, self.config.dataset)
                y_pred.append(updated_y)
                y_truth.append(y)
            node_lines.append(line_test)

        y_pred, y_truth, node_lines = torch.cat(y_pred), torch.cat(y_truth), torch.cat(node_lines)
        test_f1, test_recall, test_auc, test_MCC, test_IOU = get_accuracy(y_pred, y_truth, node_lines, self.config.train)

        tqdm.write(
            f"f1: {test_f1:.4f} | recall: {test_recall:.4f} | auc: {test_auc:.4f} | "
            f"mcc: {test_MCC:.4f} | IOU: {test_IOU:.4f}"
        )
