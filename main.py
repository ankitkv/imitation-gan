from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
from six.moves import xrange
import sys

import matplotlib
matplotlib.use('Agg')  # allows for saving images without display
import matplotlib.pyplot as plt
import matplotlib.cm as cm

import numpy as np
import torch
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import util


class Actor(nn.Module):
    '''The imitation GAN policy network.'''

    def __init__(self, opt):
        super(Actor, self).__init__()
        self.opt = opt
        self.embedding = nn.Embedding(opt.vocab_size, opt.emb_size)
        self.cell = nn.GRUCell(opt.emb_size, opt.actor_hidden_size)
        self.dist1 = nn.Linear(opt.actor_hidden_size, opt.emb_size)
        self.dist2 = nn.Linear(opt.emb_size, opt.vocab_size)
        self.embedding.weight = self.dist2.weight  # tie weights
        self.zero_input = torch.LongTensor(opt.batch_size).zero_().cuda()
        self.zero_state = torch.zeros([opt.batch_size, opt.actor_hidden_size]).cuda()
        self.eps_sample = True  # do eps sampling

    def forward(self):
        outputs = []
        corrections = []
        all_logprobs = []
        all_probs = []
        probs = []  # for debugging
        hidden = Variable(self.zero_state)
        inputs = self.embedding(Variable(self.zero_input))
        for out_i in xrange(self.opt.seq_len):
            hidden = self.cell(inputs, hidden)
            dist = F.log_softmax(self.dist2(self.dist1(hidden)))
            all_logprobs.append(dist.unsqueeze(1))
            prob = torch.exp(dist)
            all_probs.append(prob.unsqueeze(1))
            dist_new = dist.detach()
            probs.append(prob.data.mean(0).squeeze(0).cpu().numpy())  # for debugging
            # eps sampling
            if self.eps_sample:
                dist_new = dist_new.clone()
                draw_randomly = self.opt.eps >= torch.rand([self.opt.batch_size])
                draw_randomly = draw_randomly.byte().unsqueeze(1).cuda().expand_as(dist_new)
                # set uniform distribution with opt.eps probability
                dist_new[draw_randomly] = -np.log(self.opt.vocab_size)
            # torch.multinomial is broken, so this is the workaround  TODO change now
            _, sampled = torch.max(dist_new.data -
                                   torch.log(-torch.log(torch.rand(*dist_new.size()).cuda())), 1)
            sampled = Variable(sampled)
            onpolicy_prob = prob.gather(1, sampled).detach()
            if self.eps_sample:
                offpolicy_prob = torch.exp(dist_new.gather(1, sampled))
            else:
                offpolicy_prob = onpolicy_prob
            # avoid 0/0
            onpolicy_prob = onpolicy_prob.clamp(1e-8, 1.0)
            offpolicy_prob = offpolicy_prob.clamp(1e-8, 1.0)
            outputs.append(sampled)
            # use importance sampling to correct for eps sampling
            corrections.append(onpolicy_prob / offpolicy_prob)
            if out_i < self.opt.seq_len - 1:
                inputs = self.embedding(sampled.squeeze(1))
        return (torch.cat(outputs, 1), torch.cat(corrections, 1), torch.cat(all_logprobs, 1),
                torch.cat(all_probs, 1), np.array(probs))


class Critic(nn.Module):
    '''The imitation GAN discriminator/critic.'''

    def __init__(self, opt):
        super(Critic, self).__init__()
        self.opt = opt
        self.embedding = nn.Embedding(opt.vocab_size, opt.emb_size)
        self.rnn = nn.GRU(input_size=opt.emb_size, hidden_size=opt.critic_hidden_size,
                          num_layers=opt.critic_layers, dropout=opt.critic_dropout,
                          batch_first=True)
        self.cost = nn.Linear(opt.critic_hidden_size, opt.vocab_size)
        self.zero_input = torch.LongTensor(opt.batch_size, 1).zero_().cuda()
        self.zero_state = torch.zeros([opt.critic_layers, opt.batch_size,
                                       opt.critic_hidden_size]).cuda()
        self.gamma = opt.gamma

    def forward(self, actions):
        actions = Variable(actions, requires_grad=True)
        padded_actions = torch.cat([Variable(self.zero_input), actions], 1)
        inputs = self.embedding(padded_actions)
        outputs, _ = self.rnn(inputs, Variable(self.zero_state))
        outputs = outputs.contiguous()
        flattened = outputs.view(-1, self.opt.critic_hidden_size)
        flat_costs = self.cost(flattened)
        costs = flat_costs.view(self.opt.batch_size, self.opt.seq_len + 1, self.opt.vocab_size)
        costs = costs[:, :-1]  # account for the padding
        if self.gamma < 1.0 - 1e-8:
            discount = torch.cuda.FloatTensor([self.gamma ** i for i in xrange(self.opt.seq_len)])
            discount = discount.unsqueeze(0).expand(self.opt.batch_size, self.opt.seq_len)
            discount = Variable(discount)
            costs = costs * discount
        costs_abs = torch.abs(costs)
        if self.opt.smooth_zero > 1e-4:
            select = (costs_abs >= self.opt.smooth_zero).float()
            costs_abs = costs_abs - (self.opt.smooth_zero / 2)
            costs_sq = (costs ** 2) / (self.opt.smooth_zero * 2)
            return (select * costs_abs) + ((1.0 - select) * costs_sq), actions.grad
        else:
            return costs_abs, actions.grad


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--niter', type=int, default=1000000, help='number of iters to train for')
    parser.add_argument('--batch_size', type=int, default=32, help='batch size')
    parser.add_argument('--seq_len', type=int, default=20, help='toy sequence length')
    parser.add_argument('--vocab_size', type=int, default=200, help='vocab size for data')
    parser.add_argument('--emb_size', type=int, default=160, help='embedding size')
    parser.add_argument('--actor_hidden_size', type=int, default=224, help='Actor RNN hidden size')
    parser.add_argument('--critic_hidden_size', type=int, default=224,
                        help='Critic RNN hidden size')
    parser.add_argument('--critic_layers', type=int, default=1)  # TODO add actor_layers
    parser.add_argument('--critic_dropout', type=float, default=0.0)  # TODO add actor_dropout
    parser.add_argument('--eps', type=float, default=0.0, help='epsilon for eps sampling')
    parser.add_argument('--gamma', type=float, default=1.0, help='discount factor')
    parser.add_argument('--gamma_inc', type=float, default=0.0,
                        help='the amount by which to increase gamma at each turn')
    parser.add_argument('--entropy_reg', type=float, default=1.0,
                        help='policy entropy regularization')
    parser.add_argument('--critic_entropy_reg', type=float, default=0.0,
                        help='critic entropy regularization')
    parser.add_argument('--smooth_zero', type=float, default=1.0,
                        help='s, use c^2/2s instead of c-(s/2) when critic score c<s')
    parser.add_argument('--use_advantage', type=int, default=1)
    parser.add_argument('--exp_replay_buffer', type=int, default=0,
                        help='use a replay buffer with an exponential distribution')
    parser.add_argument('--real_multiplier', type=float, default=5.0,
                        help='weight for real samples as compared to fake for critic learning')
    parser.add_argument('--replay_actors', type=int, default=10,  # higher with exp buffer
                        help='number of actors for experience replay')
    parser.add_argument('--replay_actors_half', type=int, default=3,
                        help='number of recent actors making up half of the exponential replay')
    parser.add_argument('--solved_threshold', type=int, default=200,
                        help='conseq steps the task (if appl) has been solved for before exit')
    parser.add_argument('--solved_max_fail', type=int, default=10,
                        help='maximum number of failures before solved streak is reset')
    parser.add_argument('--optimizer', type=str, default='Adam')
    parser.add_argument('--learning_rate', type=float, default=1e-4)
    parser.add_argument('--beta1', type=float, default=0.5)
    parser.add_argument('--beta2', type=float, default=0.9)
    # XXX since we're not interpolating between real/fake, should this be higher?
    parser.add_argument('--gradient_penalty', type=float, default=10)
    parser.add_argument('--max_grad_norm', type=float, default=5.0,
                        help='norm for gradient clipping')
    parser.add_argument('--critic_iters', type=int, default=5,  # 20 or 25 for larger tasks
                        help='number of critic iters per turn')
    parser.add_argument('--actor_iters', type=int, default=1,  # 15 or 20 for larger tasks
                        help='number of actor iters per turn')
    parser.add_argument('--burnin', type=int, default=25, help='number of burnin iterations')
    parser.add_argument('--burnin_actor_iters', type=int, default=1)
    parser.add_argument('--burnin_critic_iters', type=int, default=100)
    parser.add_argument('--name', type=str, default='default')
    parser.add_argument('--task', type=str, default='lm', help='one of lm/longterm/words')
    parser.add_argument('--lm_data_dir', type=str, default='data/penn')
    parser.add_argument('--lm_char', type=int, default=0, help='1 for character level model')
    parser.add_argument('--print_every', type=int, default=25,
                        help='print losses every these many steps')
    parser.add_argument('--plot_every', type=int, default=1,
                        help='plot losses every these many steps')
    parser.add_argument('--gen_every', type=int, default=50,
                        help='generate sample every these many steps')
    opt = parser.parse_args()
    print(opt)

    # some logging stuff
    opt.save = 'logs/' + opt.name
    if not os.path.exists(opt.save):
        os.makedirs(opt.save)
    train_log = open(opt.save + '/train.log', 'w')
    colors = cm.rainbow(np.linspace(0, 1, 3))
    plot_r = []
    plot_f = []
    plot_w = []
    plot_cgnorm = []
    plot_agnorm = []

    opt.replay_size = opt.replay_actors * opt.batch_size * opt.critic_iters
    opt.replay_size_half = opt.replay_actors_half * opt.batch_size * opt.critic_iters

    cudnn.benchmark = True
    np.set_printoptions(precision=4, threshold=10000, linewidth=200, suppress=True)

    if opt.task == 'words':
        task = util.WordsTask(opt.seq_len, opt.vocab_size)
    elif opt.task == 'longterm':
        task = util.LongtermTask(opt.seq_len, opt.vocab_size)
    elif opt.task == 'lm':
        task = util.LMTask(opt.seq_len, opt.vocab_size, opt.lm_data_dir, opt.lm_char)
        if task.vocab_size != opt.vocab_size:
            opt.vocab_size = task.vocab_size
            print('Updated vocab_size:', opt.vocab_size)
    else:
        print('error: invalid task name:', opt.task)
        sys.exit(1)

    actor = Actor(opt)  #.apply(util.weights_init)
    critic = Critic(opt)  #.apply(util.weights_init)
    actor.cuda()
    critic.cuda()

    assert opt.replay_size >= opt.batch_size
    if opt.exp_replay_buffer:
        buffer = util.ExponentialReplayMemory(opt.replay_size, opt.replay_size_half)
    else:
        buffer = util.ReplayMemory(opt.replay_size)

    actor_optimizer = getattr(optim, opt.optimizer)(actor.parameters(), lr=opt.learning_rate,
                                                    betas=(opt.beta1, opt.beta2))
    critic_optimizer = getattr(optim, opt.optimizer)(critic.parameters(), lr=opt.learning_rate,
                                                     betas=(opt.beta1, opt.beta2))
    solved = 0
    solved_fail = 0

    print('\nReal examples:')
    task.display(task.get_data(opt.batch_size))
    print()
    plot_x = []
    for cur_iter in xrange(opt.niter):
        if solved >= opt.solved_threshold:
            print('%d: Task solved, exiting.' % cur_iter)
            break
        actor.eps_sample = opt.eps > 1e-8

        # train critic
        for param in critic.parameters():  # reset requires_grad
            param.requires_grad = True  # they are set to False below in actor update
        if cur_iter < opt.burnin:
            critic_iters = opt.burnin_critic_iters
        else:
            critic_iters = opt.critic_iters
        Wdists = []
        err_r = []
        err_f = []
        critic_gnorms = []
        for critic_i in xrange(critic_iters):
            critic.zero_grad()

            # eps sampling here can help the critic get signal from less likely actions as well.
            # corrections would ensure that the critic doesn't have to worry about such actions
            # too much though.
            generated, corrections, _, _, _ = actor()
            buffer.push(generated.data.cpu().numpy(), corrections.data.cpu().numpy())
            generated, corrections = buffer.sample(opt.batch_size)
            generated = torch.from_numpy(generated).cuda()
            corrections = Variable(torch.from_numpy(corrections).cuda())
            costs, gradient = critic(generated)
            print(gradient)  # TODO remove
            costs = costs.gather(2, Variable(generated.unsqueeze(2))).squeeze(2)
            entropy = -((1e-6 + costs) * torch.log(1e-6 + costs)).sum() / opt.batch_size
            E_generated = (costs * corrections).sum() / opt.batch_size
            loss = -E_generated - (opt.critic_entropy_reg * entropy) + \
                   (opt.gradient_penalty * gradient)  # FIXME
            loss.backward()

            real = torch.from_numpy(task.get_data(opt.batch_size)).cuda()
            costs, gradient = critic(real)
            costs = costs.gather(2, Variable(real.unsqueeze(2))).squeeze(2)
            entropy = -((1e-6 + costs) * torch.log(1e-6 + costs)).sum() / opt.batch_size
            E_real = costs.sum() / opt.batch_size
            loss = (opt.real_multiplier * E_real) - (opt.critic_entropy_reg * entropy) + \
                   (opt.gradient_penalty * gradient)  # FIXME
            loss.backward()

            critic_gnorms.append(util.gradient_norm(critic.parameters()))  # TODO not needed now
            nn.utils.clip_grad_norm(critic.parameters(), opt.max_grad_norm)
            critic_optimizer.step()
            Wdist = (E_generated - E_real).data[0]
            Wdists.append(Wdist)
            err_r.append(E_real.data[0])
            err_f.append(E_generated.data[0])

        # train actor
        for param in critic.parameters():
            param.requires_grad = False  # to avoid computation
        if cur_iter < opt.burnin:
            actor_iters = opt.burnin_actor_iters
        else:
            actor_iters = opt.actor_iters
        if cur_iter % opt.gen_every == 0:
            # disable eps_sample since we intend to visualize a (noiseless) generation.
            print_generated = True
            actor.eps_sample = False
        else:
            print_generated = False

        actor_gnorms = []
        for actor_i in xrange(actor_iters):
            actor.zero_grad()
            generated, corrections, all_logprobs, all_probs, avgprobs = actor()
            if print_generated:  # last sample is real, for debugging
                generated.data[-1].copy_(torch.from_numpy(task.get_data(1)).cuda())
            logprobs = all_logprobs.gather(2, generated.unsqueeze(2)).squeeze(2)
            costs, _ = critic(generated.data)
            if opt.use_advantage:
                baseline = (costs * all_probs).detach().sum(2).expand_as(costs)
                disadv = costs - baseline
            else:
                disadv = costs
            if print_generated:  # do not train on real sample
                corrections[-1].data.zero_()
                all_logprobs = all_logprobs[:-1]
                all_probs = all_probs[:-1]
            costs = costs.gather(2, generated.unsqueeze(2)).squeeze(2)
            disadv = disadv.gather(2, generated.unsqueeze(2)).squeeze(2)
            loss = (disadv * corrections * logprobs).sum() / opt.batch_size
            entropy = -(all_probs * all_logprobs).sum() / opt.batch_size
            loss -= opt.entropy_reg * entropy
            loss.backward()
            actor_gnorms.append(util.gradient_norm(actor.parameters()))
            nn.utils.clip_grad_norm(actor.parameters(), opt.max_grad_norm)
            actor_optimizer.step()
            if print_generated:
                # print generated only in the first actor iteration
                print('Generated:')
                task.display(generated.data.cpu().numpy())
                print()
                print('Critic costs:')
                print(costs.data.cpu().numpy(), '\n')
                print('Critic cost sums:')
                print(costs.data.cpu().numpy().sum(1), '\n')
                if opt.use_advantage:
                    print('Critic advantages:')
                    print(-disadv.data.cpu().numpy(), '\n')
                if opt.task == 'longterm':
                    print('Batch-averaged step-wise probs:')
                    print(avgprobs, '\n')
                print_generated = False
                actor.eps_sample = opt.eps > 1e-8
        critic.gamma = min(critic.gamma + opt.gamma_inc, 1.0)

        if cur_iter % opt.print_every == 0:
            print(cur_iter, ':\tWdist:', np.array(Wdists).mean(), '\terr R:',
                  np.array(err_r).mean(), '\terr F:', np.array(err_f).mean(), '\tgamma:',
                  critic.gamma, '\tsolved:', solved, '\tsolved_fail:', solved_fail)
            train_log.write('%.4f\t%.4f\t%.4f\n' % (np.array(Wdists).mean(), np.array(err_r).mean(),
                            np.array(err_f).mean()))
            train_log.flush()
        if cur_iter % opt.plot_every == 0:
            plot_x.append(cur_iter)
            plot_r.append(np.array(err_r).mean())
            plot_f.append(np.array(err_f).mean())
            plot_w.append(np.array(Wdists).mean())
            fig = plt.figure()
            x_array = np.array(plot_x)
            plt.plot(x_array, np.array(plot_w), c=colors[0])
            plt.plot(x_array, np.array(plot_r), c=colors[1])
            plt.plot(x_array, np.array(plot_f), c=colors[2])
            plt.legend(['W dist', 'D(real)', 'D(fake)'], loc=2)
            fig.savefig(opt.save + '/train.png')
            plt.close()

            plot_agnorm.append(np.array(actor_gnorms).mean())
            plot_cgnorm.append(np.array(critic_gnorms).mean())
            fig = plt.figure()
            plt.plot(x_array, np.array(plot_agnorm), c=colors[0])
            plt.plot(x_array, np.array(plot_cgnorm), c=colors[1])
            plt.legend(['Actor grad norm', 'Critic grad norm'], loc=2)
            fig.savefig(opt.save + '/grads.png')
            plt.close()

        if opt.task == 'longterm':
            params = [avgprobs]
        elif opt.task == 'words':
            generated = generated.data.cpu().numpy()
            if print_generated and actor_iters == 1:
                generated = generated[:-1]
            params = [generated]
        else:
            params = [None]
        if task.solved(*params):
            solved += 1
        else:
            reset = True
            if solved > 0:
                reset = False
                solved_fail += 1
                if solved_fail >= opt.solved_max_fail:
                    reset = True
            if reset:
                solved = 0
                solved_fail = 0
