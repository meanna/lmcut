import argparse
import torch
import math
import time
import random
import get_corpus_lm, util, data_LM
from torch.autograd import Variable
from timeit import default_timer as timer
from datetime import timedelta

start = timer()
timestr = time.strftime("%Y-%m-%d_%H.%M.%S")

parser = argparse.ArgumentParser()

# model name and imported model
parser.add_argument("--load_from", type=str)
parser.add_argument("--save_to", type=str, default="LM_" + timestr)

# model parameters
parser.add_argument("--lstm_num_direction", type=int, default=1)
parser.add_argument("--batchSize", type=int, default=128)
parser.add_argument("--sequence_length", type=int, default=80)

# layer and dimensions
parser.add_argument("--char_embedding_size", type=int, default=10)
parser.add_argument("--hidden_dim", type=int, default=10)
parser.add_argument("--layer_num", type=int, default=2)

# dropout
parser.add_argument("--char_dropout_prob", type=float, default=random.choice([0.01]))
parser.add_argument("--clip_grad", type=float, default=0.5)

# training parameters
parser.add_argument("--learning_rate", type=float, default=1)
parser.add_argument("--optim", type=str, default="adam")  # sgd or adam
parser.add_argument("--sgd_momentum", type=float, default=0.02)  # 0.02, 0.9
parser.add_argument("--adam_lr_decay", type=float, default=0.00)
parser.add_argument("--lr_decay", type=float, default=0.01)
parser.add_argument("--epoch", type=int, default=1)

# dataset parameters
parser.add_argument("--dataset", type=str, default="small")
parser.add_argument("--len_lines_per_chunk", type=int, default=100)

# log file parameters
parser.add_argument("--over_write", type=int, default=0)
parser.add_argument("--add_note", type=str)

args = parser.parse_args()
args_dict = vars(args)

import set_path
CHECKPOINTS_LM = set_path.CHECKPOINTS_LM#"./checkpoints_LM/"
train = True

if args.load_from is None:
    print("-----------start training the language model----------")
else:
    print("-----------resume training the language model--------")

# set a default note
if args.add_note is None:
    args.add_note = str(args.dataset)+ " , "+ str(args.learning_rate)+ ", epoch "+ str(args.epoch)
print(args.add_note)

dataset = args.dataset
bi_lstm = args.lstm_num_direction == 2
adam_with_lr_decay = args.adam_lr_decay != 0

# when resume the training, save new weights to the resumed model instead of creating a new model
if args.load_from is not None and args.over_write == 1:
    print("overwrite new weights to the resumed model")
    args.save_to = args.load_from

print("model name: " , args.save_to)
cuda = torch.cuda.is_available()
print("cuda: ,", torch.cuda.is_available())

itos = data_LM.itos
util.export_args(args_dict, CHECKPOINTS_LM + args.save_to)

class Model:
    """
    define the model and download the weights if available.
    define forward and backward
    """

    def __init__(self, bi_lstm):

        if cuda:
            if bi_lstm:
                self.rnn = torch.nn.LSTM(args.char_embedding_size, args.hidden_dim, args.layer_num,
                                         bidirectional=True).cuda()
                self.output = torch.nn.Linear(args.hidden_dim * 2, len(itos)).cuda()
            else:
                self.rnn = torch.nn.LSTM(args.char_embedding_size, args.hidden_dim, args.layer_num).cuda()
                self.output = torch.nn.Linear(args.hidden_dim, len(itos)).cuda()
            self.char_embeddings = torch.nn.Embedding(num_embeddings=len(itos),
                                                      embedding_dim=args.char_embedding_size).cuda()
        else:
            if bi_lstm:
                self.rnn = torch.nn.LSTM(args.char_embedding_size, args.hidden_dim, args.layer_num, bidirectional=True)
                self.output = torch.nn.Linear(args.hidden_dim * 2, len(itos))
            else:
                self.rnn = torch.nn.LSTM(args.char_embedding_size, args.hidden_dim, args.layer_num)
                self.output = torch.nn.Linear(args.hidden_dim, len(itos))

            self.char_embeddings = torch.nn.Embedding(num_embeddings=len(itos), embedding_dim=args.char_embedding_size)

        self.modules = [self.rnn, self.output, self.char_embeddings]

        self.parameters_cached = [x for x in self.parameters(self.modules)]

        learning_rate = args.learning_rate
        if args.optim == "adam":
            self.optim = torch.optim.Adam(self.parameters(self.modules), lr=learning_rate)
        else:
            self.optim = torch.optim.SGD(self.parameters(self.modules), lr=learning_rate,
                                         momentum=args.sgd_momentum)  # 0.02, 0.9

        # do not save "optim" parameter
        self.named_modules = {"rnn": self.rnn, "output": self.output, "char_embeddings": self.char_embeddings}

        if args.load_from is not None:
            checkpoint = torch.load(CHECKPOINTS_LM + args.load_from + ".pth.tar")
            for name, module in self.named_modules.items():
                module.load_state_dict(checkpoint[name])
            print("load parameters and weights....")

    def parameters(self, modules):
        for module in modules:
            for param in module.parameters(modules):
                yield param

    def forward(self, numeric, train=True):
        global hidden
        global beginning
        if hidden is None or (train and random.random() > 0.9):
            hidden = None
            beginning = zeroBeginning
        elif hidden is not None:
            hidden = tuple([Variable(x.data).detach() for x in hidden])

        numeric = torch.cat([beginning, numeric], dim=0)
        beginning = numeric[numeric.size()[0] - 1].view(1, args.batchSize)

        input_tensor = Variable(numeric[:-1], requires_grad=False)
        target_tensor = Variable(numeric[1:], requires_grad=False)

        embedded = self.char_embeddings(input_tensor)
        if train:
            embedded = char_dropout(embedded)

        out, hidden = self.rnn(embedded, hidden)  # --------- training

        logits = self.output(out)
        log_probs = logsoftmax(logits)
        loss = train_loss(log_probs.view(-1, len(itos)), target_tensor.view(-1))

        return loss, target_tensor.view(-1).size()[0]

    def backward(self, loss):
        self.optim.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_value_(self.parameters_cached, clip_value=args.clip_grad)
        self.optim.step()


def save_log(mode="w"):
    with open(CHECKPOINTS_LM + args.save_to, mode) as outFile:
        if mode == "a":
            print("----------resume the training ---------", file=outFile)
        else:
            print("-----------Language Model---------", file=outFile)
        print("file name = ", CHECKPOINTS_LM + args.save_to, file=outFile)
        print("", file=outFile)
        long, short = util.get_command(str(args))
        p = "python LanguageModel.py "
        long = p + long
        short = p + short
        print(">>> command with full parameters", file=outFile)
        print(long, file=outFile)
        print("\n>>> command with short parameters", file=outFile)
        print(short, file=outFile)
        print("", file=outFile)

        print(">>> ", args.add_note, file=outFile)
        print("trainLosses ", trainLosses, file=outFile)
        print("devLosses ", devLosses, file=outFile)
        print("", file=outFile)
        print("count train sample ", count_train_samples, file=outFile)
        print("count dev sample ", count_dev_samples, file=outFile)

        print("", file=outFile)
        print("", file=outFile)
        l = str(args)[10:].strip(")").split(",")

        for i in l:
            print(i, file=outFile)

        print("", file=outFile)
        print("train set: ", train_path, file=outFile)
        print("dev set: ", dev_path, file=outFile)
        print("", file=outFile)
        print("config for later download : ", file=outFile)
        p = util.get_param(str(args))
        print(p, file=outFile)
        print("", file=outFile)
        print("save log file to ", args.save_to)

# append the result in "LM_log.csv"
def save_csv(f= "LM_log.csv"):
    with open(CHECKPOINTS_LM + f, "a") as table:
        print("---------save training results------")
        print(args.save_to, file=table, end=';')
        print(args.dataset, file=table, end=';')
        print(num_epoch + 1, file=table, end=';')
        print("trainLosses ", trainLosses, file=table, end=';')
        print("devLosses ", devLosses, file=table, end=';')
        print(args.add_note, file=table, end=';')
        p = util.get_param(str(args))
        print(p, file=table, end=';')
        long, short = util.get_command(str(args))
        p = "python LanguageModel.py "
        long = p + long
        print(long, file=table, end='\n')


# model training
count_train_samples = 0
count_dev_samples = 0
num_epoch=1
if train:
    model = Model(bi_lstm)
    train_path, dev_path, test_path = get_corpus_lm.get_path_data(dataset)
    if cuda:
        zeroBeginning = torch.LongTensor([2 for _ in range(args.batchSize)]).cuda().view(1, args.batchSize)
    else:
        zeroBeginning = torch.LongTensor([2 for _ in range(args.batchSize)]).view(1, args.batchSize)

    logsoftmax = torch.nn.LogSoftmax(dim=2)
    train_loss = torch.nn.NLLLoss(ignore_index=2)
    print_loss = torch.nn.NLLLoss(size_average=False, reduce=False)
    char_dropout = torch.nn.Dropout2d(p=args.char_dropout_prob)
    trainLosses = []
    devLosses = []
    for epoch in range(args.epoch):
        print("epoch: ", epoch + 1)
        training_data = get_corpus_lm.load(train_path, doShuffling=True, len_chunk=args.len_lines_per_chunk)
        print("Got the training data")
        training_chars = data_LM._prepareDatasetChunks(args, training_data)

        model.rnn.train(True)
        startTime = time.time()
        trainChars = 0
        train_loss_ = 0
        counter = 0
        hidden, beginning = None, None
        while True:
            counter += 1
            try:
                numeric = next(training_chars)
            except StopIteration:
                print("end of the batch")
                break

            loss, charCounts = model.forward(numeric, train=True)  # ---- training
            if epoch == 0:
                count_train_samples += args.batchSize

            model.backward(loss)

            train_loss_ += charCounts * loss.cpu().data.numpy()
            trainChars += charCounts

        trainLosses.append(train_loss_ / trainChars)
        print("trainLosses ", trainLosses)

        if True:
            print("save model parameters... ")
            torch.save(dict([(name, module.state_dict()) for name, module in model.named_modules.items()]),
                       CHECKPOINTS_LM + args.save_to + ".pth.tar")
            save_csv(f= "LM_log_temp.csv")

        model.rnn.train(False)

        dev_data = get_corpus_lm.load(dev_path, len_chunk=args.len_lines_per_chunk, doShuffling=False)
        print("Got dev data")
        dev_chars = data_LM._prepareDatasetChunks(args, dev_data)

        dev_loss = 0
        dev_char_count = 0
        counter = 0
        hidden, beginning = None, None
        while True:
            counter += 1
            try:
                numeric = next(dev_chars)
            except StopIteration:
                break

            loss, numberOfCharacters = model.forward(numeric, train=False)
            dev_loss += numberOfCharacters * loss.cpu().data.numpy()
            dev_char_count += numberOfCharacters
            if epoch == 0:
                count_dev_samples += args.batchSize

        devLosses.append(dev_loss / dev_char_count)
        print("dev losses ", devLosses)

        # if resume the training, append log data only for the last iteration
        if args.load_from is None:
            save_log("w")
        elif args.load_from is not None and epoch >= args.epoch - 1:
            save_log("a")
        if len(devLosses) > 1 and devLosses[-1] >= devLosses[-2]:
            print("early stopping")
            with open(CHECKPOINTS_LM + args.save_to, "a") as outFile:
                print("early stopping ",  file=outFile)
            break

        end = timer()
        total_time = timedelta(seconds=end - start)
        if True:
            print("save model parameters... ")
            torch.save(dict([(name, module.state_dict()) for name, module in model.named_modules.items()]),
                       CHECKPOINTS_LM + args.save_to + ".pth.tar")
            save_csv(f= "LM_log_temp.csv")

        if args.optim == "adam" and adam_with_lr_decay:
            learning_rate = args.learning_rate * math.pow(args.adam_lr_decay, len(devLosses))
            optim = torch.optim.Adam(model.parameters(model.modules), lr=learning_rate)
        elif args.optim == "sgd":
            learning_rate = args.learning_rate * math.pow(args.lr_decay, len(devLosses))
            optim = torch.optim.SGD(model.parameters(model.modules), lr=learning_rate,
                                    momentum=args.sgd_momentum)

end = timer()
total_time = timedelta(seconds=end - start)
print(timedelta(seconds=end - start))
save_csv()
print()
config = args
p = util.get_param(str(args))
print("config for later download")
print(p)
print(args.add_note)
print("count train sample ", count_train_samples)
print("count dev sample ", count_dev_samples)