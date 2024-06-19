# RNN-SAC
A discreet SAC with RNN (LSTM) for the policy network mainly copied from https://github.com/RobvanGastel/rnn-sac.

# Current work
An extension of the RNN-SAC for meta-learning. I am implementing a 2nd level of RL (again SAC) to choose between previously learned (weight frozen) SAC networks in order to collect experiences whilst learning a new SAC. The idea is taken from the fact that animals do not seem to learn new tasks by randomply exressing actions but by testing out policies learned previously in similar tasks.

## References
- Haarnoja, T., Zhou, A., Hartikainen, K., Tucker, G., Ha, S., Tan, J., Kumar, V., Zhu, H., Gupta, A., Abbeel, P., & Levine, S. (2019). Soft Actor-Critic Algorithms and Applications (arXiv:1812.05905). arXiv. https://doi.org/10.48550/arXiv.1812.05905
- Wang, J. X., Kurth-Nelson, Z., Tirumala, D., Soyer, H., Leibo, J. Z., Munos, R., Blundell, C., Kumaran, D., & Botvinick, M. (2017). Learning to reinforcement learn. ArXiv:1611.05763 [Cs, Stat]. http://arxiv.org/abs/1611.05763
- Rakelly, K., Zhou, A., Quillen, D., Finn, C., & Levine, S. (2019). Efficient Off-Policy Meta-Reinforcement Learning via Probabilistic Context Variables (arXiv:1903.08254). arXiv. https://doi.org/10.48550/arXiv.1903.08254
- Lin, Z., Li, J., Shi, J., Ye, D., Fu, Q., and Yang, W. (2021). JueWu-MC: Playing Minecraft with Sample-efficient Hierarchical Reinforcement Learning. Preprint at arXiv.
 
