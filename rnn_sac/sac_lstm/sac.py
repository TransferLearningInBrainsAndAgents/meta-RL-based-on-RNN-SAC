
import copy
import time
import itertools
import numpy as np

import torch
import torch.nn as nn
from torch.optim import Adam
from torch.optim.lr_scheduler import StepLR
from torch.utils.tensorboard import SummaryWriter

from rnn_sac.sac_lstm.core import ActorCritic, count_vars
from rnn_sac.sac_lstm.buffer import EpisodicBuffer
from rnn_sac.utils.logx import EpochLogger


class SAC:
    def __init__(self, env, logger_kwargs=dict(), seed=42, max_ep_len=2000,
                 save_every_n_update=1, gamma=0.99, lr=1e-4, gamma_lr=0.5, epochs_to_update_lr=2,
                 polyak=0.995, epochs=1, batch_size=16, hidden_size=256,
                 start_steps=1000, update_every=50,
                 exploration_sampling=False, clip_ratio=1.0,
                 number_of_trajectories=100, use_alpha_annealing=False,
                 entropy_target_mult=0.98,
                 model_file_to_load=None):

        self.logger = EpochLogger(**logger_kwargs)
        self.logger.save_config(locals())

        self.summary_writer = SummaryWriter(
            log_dir=logger_kwargs['output_dir'],
            flush_secs=1)

        torch.manual_seed(seed)
        np.random.seed(seed)

        self.save_freq = save_every_n_update * update_every

        self.max_ep_len = max_ep_len

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")

        # Meta-learning parameters
        self.epochs = epochs

        # Number of steps per trial
        #self.steps_per_epoch = steps_per_epoch
        #self.total_steps = steps_per_epoch * epochs
        self.number_of_trajectories = number_of_trajectories

        self.total_traj = 0
        self.current_test_epoch = 0
        self.current_epoch = 0
        self.steps_per_epoch = self.number_of_trajectories * self.max_ep_len

        # Meta-testing
        # Increase global steps for the next trial
        self.global_test_steps = 0
        self.global_steps = 0

        # Updating the network parameters
        self.lr = lr
        self.gamma = gamma
        self.polyak = polyak
        self.clip_ratio = clip_ratio
        self.update_counter = 0
        self.update_multiplier = 20
        self.start_steps = start_steps
        self.update_every = update_every

        self.batch_size = batch_size
        self.hidden_size = hidden_size
        self.model_file_to_load = model_file_to_load
        self.env = env

        # The online and target networks
        self.ac = self.create_or_load_model()
        self.ac_targ = copy.deepcopy(self.ac)

        # Set up model saving
        self.logger.setup_pytorch_saver(self.ac)

        # Freeze target networks with respect to optimizers
        # (only update via polyak averaging)
        for p in self.ac_targ.parameters():
            p.requires_grad = False

        obs_dim = env.observation_space.shape
        act_dim = env.action_space.shape

        # replay buffer to stay close to the idea of updating
        # on the whole trajectories for meta-learning purposes
        self.buffer = EpisodicBuffer(obs_dim=obs_dim, act_dim=act_dim, size=self.steps_per_epoch,
                                     hidden_size=self.hidden_size, device=self.device, use_sac=True,
                                     use_exploration_sampling=exploration_sampling)

        # Optimize entropy exploration-exploitation parameter
        self.use_alpha_annealing = use_alpha_annealing
        if use_alpha_annealing:
            self.entropy_target = entropy_target_mult * (-np.log(1 / self.env.action_space.n))
            self.log_alpha = torch.zeros(1, requires_grad=True, device=self.device)
            self.alpha = self.log_alpha.exp()
            self.alpha_optimizer = Adam([self.log_alpha], lr=self.lr)
        else:
            self.alpha = 0.2

        # List of parameters for both Q-networks (save this for convenience)
        self.q_params = itertools.chain(self.ac.q1.parameters(),
                                        self.ac.q2.parameters())

        self.pi_params = itertools.chain(self.ac.pi.parameters(),
                                         self.ac.memory.parameters())

        # Set up optimizers for policy and q-function
        self.pi_optimizer = Adam(self.pi_params, lr=self.lr)
        self.pi_scheduler = StepLR(self.pi_optimizer, step_size=epochs_to_update_lr, gamma=gamma_lr)
        self.q_optimizer = Adam(self.q_params, lr=self.lr)
        self.q_scheduler = StepLR(self.q_optimizer, step_size=epochs_to_update_lr, gamma=gamma_lr)

        # Count variables
        var_counts = tuple(count_vars(module)
                           for module in [self.ac.pi, self.ac.q1,
                                          self.ac.q2, self.ac.memory])
        self.logger.log(
            '\n# of parameters: \t pi: %d, \t q1: %d, \t q2: %d, \t mem: %d\n'
            % var_counts)

    def create_or_load_model(self):
        if self.model_file_to_load is None:
            return ActorCritic(self.env.observation_space, self.env.action_space, self.device,
                               hidden_size=self.hidden_size).to(self.device)
        else:
            return torch.load(self.model_file_to_load)

    def compute_critic_loss(self, batch):
        obs, act, rew = batch['obs'], batch['act'], batch['rew']
        next_obs, done = batch['obs2'], batch['done']

        # RL^2 variables
        h_out = batch['hid_out'].view(1, 1, self.hidden_size)

        rew = rew.view(-1, 1)

        # Current online network q values
        q1 = self.ac.q1(obs)
        q2 = self.ac.q2(obs)

        q1 = q1.gather(1, act.view(-1, 1).long())
        q2 = q2.gather(1, act.view(-1, 1).long())

        # Target actions come from *current* policy
        with torch.no_grad():
            memory_emb_pi, _ = self.ac.memory(
                next_obs, act, rew, h_out, training=True)
            _, a2, logp_a2 = self.ac.pi.sample(memory_emb_pi)

            # Target Q-values
            q1_targ = self.ac_targ.q1(next_obs)
            q2_targ = self.ac_targ.q2(next_obs)
            q_targ = torch.min(q1_targ, q2_targ)

            # To map R^|A| -> R
            next_q = (a2 * (q_targ - self.alpha * logp_a2)
                      ).sum(dim=1, keepdim=True)

        assert rew.shape == next_q.shape
        backup = rew + self.gamma * (1 - done) * next_q

        # MSE loss against Bellman backup
        loss_q1 = (q1 - backup).pow(2).mean()
        loss_q2 = (q2 - backup).pow(2).mean()
        loss_q = loss_q1 + loss_q2

        # Useful info for logging
        mean_q1 = q1.detach().mean().item()
        mean_q2 = q1.detach().mean().item()
        q_info = dict(Q1Vals=mean_q1, Q2Vals=mean_q2)
        #self.logger.store(Q1Vals=mean_q1, Q2Vals=mean_q2)

        return loss_q, q_info

    def compute_policy_loss(self, batch):
        obs = batch['obs']

        # RL^2 variables
        prev_act, prev_rew = batch['prev_act'], batch['prev_rew'].view(-1, 1)
        h_in = batch['hid'].view(1, 1, self.hidden_size)

        memory_emb, _ = self.ac.memory(
            obs, prev_act, prev_rew, h_in, training=True)
        _, pi, logp_pi = self.ac.pi.sample(memory_emb)

        with torch.no_grad():
            q1_pi = self.ac.q1(obs)
            q2_pi = self.ac.q2(obs)
            q_pi = torch.min(q1_pi, q2_pi)

        # Expectation of entropy
        entropy = -torch.sum(pi * logp_pi, dim=1, keepdim=True)
        self.logger.store(Entropy=entropy.cpu().detach().numpy())

        # Expectations of Q
        q = torch.sum(q_pi * pi, dim=1, keepdim=True)

        # Entropy-regularized policy loss
        loss_pi = (- q - self.alpha * entropy).mean()

        # Useful info for logging
        pi_info = dict(LogPi=logp_pi.cpu().detach().numpy(),
                       entropy=entropy.cpu().detach().numpy())

        return loss_pi, logp_pi, pi_info

    def update(self):
        batch = self.buffer.get(self.batch_size, p_exploration=0.1)

        for episode in batch:
            # Optimize Q-networks
            # First run one gradient descent step for Q1 and Q2
            loss_q, q_info = self.compute_critic_loss(episode)

            self.q_optimizer.zero_grad()
            loss_q.backward()
            nn.utils.clip_grad_norm_(self.ac.parameters(), self.clip_ratio)
            self.q_optimizer.step()

            # Recording Q-values
            self.logger.store(LossQ=loss_q.item(), **q_info)

            # Freeze Q-networks so you don't waste computational effort
            # computing gradients for them during the policy learning step.
            for p in self.q_params:
                p.requires_grad = False

            # Optimize the Policy
            # Next run one gradient descent step for pi.
            self.pi_optimizer.zero_grad()
            loss_pi, logp_pi, pi_info = self.compute_policy_loss(episode)

            loss_pi.backward()
            nn.utils.clip_grad_norm_(self.ac.parameters(), self.clip_ratio)
            self.pi_optimizer.step()

            # Unfreeze Q-networks so you can optimize it at next DDPG step.
            for p in self.q_params:
                p.requires_grad = True

            # Recording policy values
            self.logger.store(LossPi=loss_pi.item(), **pi_info)

            # Optimize the alpha
            # Entropy values
            if self.use_alpha_annealing:
                alpha_loss = -(self.log_alpha * (logp_pi.detach() +
                                                 self.entropy_target)).mean()

                self.alpha_optimizer.zero_grad()
                alpha_loss.backward()
                self.alpha_optimizer.step()
                self.alpha = self.log_alpha.exp()
            else:
                alpha_loss = torch.tensor(0)
                self.alpha = torch.tensor(0.2)

        # Recording alpha and alpha loss
        self.logger.store(LossAlpha=alpha_loss.cpu().detach().numpy(),
                          Alpha=self.alpha.cpu().detach().numpy())

        # Finally, update target networks by polyak averaging.
        with torch.no_grad():
            for p, p_targ in zip(self.ac.parameters(),
                                 self.ac_targ.parameters()):
                # NB: We use an in-place operations "mul_", "add_" to update
                # target params, as opposed to "mul" and "add", which would
                # make new tensors.
                p_targ.data.mul_(self.polyak)
                p_targ.data.add_((1 - self.polyak) * p.data)

    def get_action(self, obs, prev_act, prev_rew, hid_in, greedy=True):
        # obs shape: [1, obs_dim]
        obs = torch.as_tensor(
            obs, dtype=torch.float32).to(self.device).unsqueeze(0)

        # Don't unsqueeze for one-hot encoding
        # act shape: [1]
        prev_act = torch.as_tensor(
            [prev_act], dtype=torch.float32).to(self.device)

        # rew shape: [1, 1]
        prev_rew = torch.as_tensor(
            [prev_rew], dtype=torch.float32).to(self.device).unsqueeze(0)

        return self.ac.act(obs, prev_act, prev_rew, hid_in) if greedy \
            else self.ac.explore(obs, prev_act, prev_rew, hid_in)

    def test_agent(self, test_env, num_test_episodes=10, random_init=1000, greedy_ratio=0.8):
        self.test_env = test_env
        h = torch.zeros([1, 1, self.hidden_size]).to(self.device)
        observations = {}
        rewards = {}
        a2, r2 = 0, 0

        for ep in range(num_test_episodes):
            o, info = self.test_env.reset()
            d = False
            ep_len = 0
            ep_rew = 0

            observations[ep] = []
            rewards[ep] = []

            if random_init:
                for _ in range(random_init):
                    a = self.env.action_space.sample()
                    o2, r, ter, trunc, info = self.env.step(a)

            while not(d or (ep_len == self.max_ep_len)):
                greedy = True
                if np.random.random() > greedy_ratio:
                    greedy = False
                a, h = self.get_action(
                    o, a2, r2, h, greedy=greedy)

                o2, r, ter, trunc, info = self.env.step(a)
                d = ter or trunc

                o = o2
                r2 = r
                a2 = a

                observations[ep].append(o)
                rewards[ep].append(r)
                ep_rew += r
                ep_len += 1

                self.global_test_steps += 1
            self.logger.store(TestEpRew=ep_rew, TestEpLen=ep_len)
            print(ep_rew)
        self._log_test_trial(self.global_test_steps)

        return observations, rewards

    def train_agent(self, env):
        # Prepare for interaction with environment
        self.env = env
        start_time = time.time()

        # RL^2 variables
        a2, r2 = 0, 0

        for epoch in range(self.epochs):
            # Inbetween trials reset the hidden weights
            h_in = torch.zeros([1, 1, self.hidden_size]).to(self.device)
            h_out = torch.zeros([1, 1, self.hidden_size]).to(self.device)

            # To sample k trajectories
            for tr in range(self.number_of_trajectories):
                d, ep_ret, ep_len, ep_max_acc = False, 0, 0, 0
                o, info = self.env.reset()
                print('Trajectory = {}, Total steps = {}'.format(tr, self.global_steps))
                while not(d or (ep_len == self.max_ep_len)):
                    # Keeping track of current hidden states
                    h_in = h_out

                    # Until start_steps have elapsed, randomly sample actions
                    # from a uniform distribution for better exploration.
                    # Afterwards, use the learned policy.
                    if self.global_steps > self.start_steps:
                        a, h_out = self.get_action(
                            o, a2, r2, h_in, greedy=False)
                    else:
                        a = self.env.action_space.sample()

                    o2, r, ter, trunc, info = self.env.step(a)
                    d = ter or trunc
                    ep_ret += r
                    ep_len += 1

                    # Ignore the "done" signal if it comes from hitting the
                    # time horizon (that is, when it's an artificial terminal
                    # signal that isn't based on the agent's state)
                    d = False if ep_len == self.max_ep_len else d

                    self.buffer.store(
                        o, o2,
                        a, r, d,
                        a2, r2,
                        (h_in.cpu().numpy(), h_out.cpu().numpy()))

                    # Super critical, easy to overlook step: make sure to
                    # update most recent observation!
                    o = o2
                    # Set previous action and reward
                    r2 = r
                    a2 = a

                    # End of trajectory handling
                    if d or (ep_len == self.max_ep_len):
                        self.buffer.finish_path()

                        if d or (ep_len == self.max_ep_len):
                            self.logger.store(EpRew=ep_ret, EpLen=ep_len,
                                              EpMaxAcc=ep_max_acc)
                    # Increase global steps for the next trial
                    self.global_steps += 1

                # Update handling
                if (tr+1) >= self.update_every and (tr+1) % self.update_every == 0:
                    self.update()
                    self._log_trial(epoch, tr, start_time)
                print('Total reward = {}'.format(ep_ret), end='\n')

            self.buffer.reset()
            self.current_epoch += 1
            self.pi_scheduler.step()
            self.q_scheduler.step()

    def _log_trial(self, epoch, trajectory, start_time):
        #epoch = t // (self.max_ep_len * self.number_of_trajectories)
        #trajectory = (t // self.max_ep_len) - 1 - epoch * self.number_of_trajectories
        t = (epoch * self.number_of_trajectories + trajectory) * self.max_ep_len
        # Save model
        print(self.save_freq, epoch, trajectory)
        if ((trajectory + 1) % self.save_freq == 0) or (trajectory + 1 == self.number_of_trajectories):
            self.logger.save_state({'env': self.env}, '{}_{}'.format(epoch, trajectory))

        # Log info about the current trial
        log_perf_board = ['EpRew', 'EpLen', 'Q2Vals',
                          'Q1Vals', 'LogPi']
        log_loss_board = ['LossPi', 'LossQ']
        log_entropy_board = ['Entropy', 'Alpha', 'LossAlpha']
        log_board = {'Performance': log_perf_board,
                     'Loss': log_loss_board,
                     'Entropy': log_entropy_board}

        # Update tensorboard
        for key, value in log_board.items():
            for val in value:
                mean, std = self.logger.get_stats(val)

                if key == 'Performance':
                    self.summary_writer.add_scalar(
                        key+'/Average'+val, mean, t)
                    self.summary_writer.add_scalar(
                        key+'/Std'+val, std, t)
                else:
                    self.summary_writer.add_scalar(
                        key+'/'+val, mean, t)

        self.logger.log_tabular('Trial', trajectory)
        self.logger.log_tabular('EpRew', with_min_and_max=True)
        self.logger.log_tabular('EpLen', average_only=True)
        self.logger.log_tabular('TotalEnvInteracts', t)
        self.logger.log_tabular('Q2Vals', with_min_and_max=True)
        self.logger.log_tabular('Q1Vals', with_min_and_max=True)
        self.logger.log_tabular('LogPi', with_min_and_max=True)
        self.logger.log_tabular('Entropy', with_min_and_max=True)
        self.logger.log_tabular('Alpha', average_only=True)
        self.logger.log_tabular('LossAlpha', average_only=True)
        self.logger.log_tabular('LossPi', average_only=True)
        self.logger.log_tabular('LossQ', average_only=True)

        self.logger.log_tabular('Time', time.time()-start_time)
        self.logger.dump_tabular()

    def _log_test_trial(self, t):
        trial = (t+1) // self.steps_per_epoch

        # Save model
        if (trial % self.save_freq == 0) or (trial == self.epochs):
            self.logger.save_state({'env': self.env}, None)

        # Log info about the current trial
        log_board = {
            'Performance': ['TestEpRew', 'TestEpLen']}

        # Update tensorboard
        for key, value in log_board.items():
            for val in value:
                mean, std = self.logger.get_stats(val)

                if key == 'Performance':
                    self.summary_writer.add_scalar(
                        key+'/Average'+val, mean, t)
                    self.summary_writer.add_scalar(
                        key+'/Std'+val, std, t)
                else:
                    self.summary_writer.add_scalar(
                        key+'/'+val, mean, t)
