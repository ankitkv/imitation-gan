from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
from six.moves import xrange

import numpy as np
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim


class Actor(nn.Module):
    '''The imitation GAN policy network.'''

    def __init__(self, opt):
        super(Actor, self).__init__()
        self.opt = opt
        self.embedding = nn.Embedding(opt.vocab_size, opt.emb_size)
        self.cell = nn.GRUCell(opt.emb_size, opt.hidden_size)
        self.dist = nn.Linear(opt.hidden_size, opt.vocab_size)
        self.step = 0

    def forward(self):
        outputs = []
        hidden = Variable(torch.zeros([self.opt.batch_size, self.opt.hidden_size]).cuda())
        inputs = self.embedding(Variable(torch.LongTensor(self.opt.batch_size).zero_().cuda()))
        for out_i in xrange(self.opt.seq_len):
            hidden = self.cell(inputs, hidden)
            dist = F.log_softmax(self.dist(hidden)).data.cpu().numpy()

            # decide the current eps threshold based on the number of steps so far
            eps_threshold = self.opt.eps_end + (self.opt.eps_start - self.opt.eps_end) * \
                                                  np.exp(-4. * self.step / self.opt.eps_decay_steps)
            self.step += 1  # to decay eps
            draw_randomly = eps_threshold >= np.random.random_sample([self.opt.batch_size])
            # set uniform (log) probability with eps_threshold probability
            dist[draw_randomly, :] = -np.log(self.opt.vocab_size)

            # for explanation of below, see https://github.com/tensorflow/tensorflow/issues/456
            sampled = np.argmax(dist - np.log(-np.log(np.random.random_sample(dist.shape))), axis=1)
            sampled = torch.from_numpy(sampled).cuda()
            outputs.append(sampled.unsqueeze(1))
            if out_i < self.opt.seq_len - 1:
                inputs = self.embedding(Variable(sampled))
        return torch.cat(outputs, 1)


class Critic(nn.Module):
    '''The imitation GAN discriminator/critic.'''

    def __init__(self, opt):
        super(Critic, self).__init__()
        self.opt = opt
        self.embedding = nn.Embedding(opt.vocab_size, opt.emb_size)
        self.rnn = nn.GRU(input_size=opt.emb_size, hidden_size=opt.hidden_size, num_layers=1,
                          batch_first=True)
        self.cost = nn.Linear(opt.hidden_size, 1)

    def forward(self, actions):
        actions = torch.cat([torch.LongTensor(self.opt.batch_size, 1).zero_().cuda(), actions],
                            1)
        actions = Variable(actions)
        inputs = self.embedding(actions)
        outputs, _ = self.rnn(inputs)
        outputs = outputs.contiguous()
        flattened = outputs.view(-1, self.opt.hidden_size)
        flat_costs = self.cost(flattened)
        cost = flat_costs.sum() / self.opt.batch_size
        return cost


def get_toy_data(batch_size, seq_len, vocab_size):
    '''Generate very simple toy training data. Generates sequences of integers where a 'word' is
       consecutive increasing integers and 1 separates words. 0 is reserved.'''
    batch = np.zeros([batch_size, seq_len], dtype=np.int)
    cur_word = np.random.randint(1, vocab_size-1, size=batch_size, dtype=np.int)
    batch[:, 0] = cur_word + 1
    for i in xrange(1, seq_len):
        zero_mask = cur_word == 0
        cur_word += 1
        cur_word[cur_word > vocab_size-2] = 0
        cur_word *= np.random.binomial(np.ones(batch_size, dtype=np.int), 0.75)
        cur_word[zero_mask] = np.random.randint(1, vocab_size-1, size=np.sum(zero_mask),
                                                dtype=np.int)
        batch[:, i] = cur_word + 1
    return batch


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--niter', type=int, default=25, help='number of epochs to train for')
    parser.add_argument('--batch_size', type=int, default=32, help='batch size')
    parser.add_argument('--seq_len', type=int, default=25, help='toy sequence length')
    parser.add_argument('--vocab_size', type=int, default=10,
                        help='character vocab size for toy data')
    parser.add_argument('--emb_size', type=int, default=32, help='embedding size')
    parser.add_argument('--hidden_size', type=int, default=256, help='RNN hidden size')
    parser.add_argument('--eps_start', type=float, default=0.9, help='initial eps for eps-greedy')
    parser.add_argument('--eps_end', type=float, default=0.05, help='final eps for eps-greedy')
    parser.add_argument('--eps_decay_steps', type=int, default=500,
                        help='number of steps to exp decay over (4 for e^(-x))')
    parser.add_argument('--learning_rate', type=float, default=0.00005, help='learning rate')
    parser.add_argument('--clamp_lower', type=float, default=-0.01)
    parser.add_argument('--clamp_upper', type=float, default=0.01)
    parser.add_argument('--critic_iters', type=int, default=5,
                        help='number of critic iters per each actor iter')
    opt = parser.parse_args()

    actor = Actor(opt)
    critic = Critic(opt)
    actor.cuda()
    critic.cuda()

    one = torch.cuda.FloatTensor([1])
    mone = one * -1

    critic_optimizer = optim.RMSprop(critic.parameters(), lr=opt.learning_rate)

    for epoch in xrange(opt.niter):
        # train critic
        for critic_i in xrange(opt.critic_iters):
            for param in critic.parameters():
                param.data.clamp_(-1, 1)
            critic.zero_grad()

            generated = actor.forward()
            E_generated = critic(generated)
            E_generated.backward(mone)

            real = torch.from_numpy(get_toy_data(opt.batch_size, opt.seq_len,
                                                 opt.vocab_size)).cuda()
            E_real = critic(real)
            E_real.backward(one)

            critic_optimizer.step()
            Wdist = (E_generated - E_real).data[0]
            print(Wdist)
