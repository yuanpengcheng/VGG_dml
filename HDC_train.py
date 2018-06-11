# coding=utf-8
from __future__ import absolute_import, print_function
import time
import argparse
import os
import sys
import torch.utils.data
from torch.backends import cudnn
from torch.autograd import Variable
import models
import losses
from utils import multi_gpu_load, RandomIdentitySampler, \
    FastRandomIdentitySampler, mkdir_if_missing, logging, display, \
    load_parameter, set_bn_eval, save_model
import DataSet
import numpy as np
cudnn.benchmark = True


def main(args):
    s_ = time.time()

    #  训练日志保存
    log_dir = args.log_dir
    mkdir_if_missing(log_dir)

    sys.stdout = logging.Logger(os.path.join(log_dir, 'log.txt'))

    display(args)

    model = models.create(args.net)

    if args.r is None:
        # load part of the model
        model = load_parameter(model)
        print('Load model Done!')
    else:
        # resume model
        print('Resume from model at Epoch %d' % args.start)
        model.load_state_dict(multi_gpu_load(args.r))

    model = model.cuda()
    model = torch.nn.DataParallel(model)

    # freeze BN
    if args.BN == 1:
        print(40 * '#', 'BatchNorm frozen')
        model.apply(set_bn_eval)
    else:
        print(40*'#', 'BatchNorm NOT frozen')
    # Fine-tune the model: the learning rate for pre-trained parameter is 1/10

    new_param_ids = set(map(id, model.module.Embeddings.parameters()))

    new_params = [p for p in model.module.parameters() if
                  id(p) in new_param_ids]

    base_params = [p for p in model.module.parameters() if
                   id(p) not in new_param_ids]

    param_groups = [
                {'params': base_params, 'lr_mult': 0.0},
                {'params': new_params, 'lr_mult': 1.0}]

    # save_model(model, os.path.join(log_dir, 'model.pth'))
    print('initial model is save at %s' % log_dir)

    optimizer = torch.optim.Adam(param_groups, lr=args.lr,
                                 weight_decay=args.weight_decay)

    if args.loss == 'center-nca':
        criterion = losses.create(args.loss, alpha=args.alpha).cuda()
    elif args.loss == 'cluster-nca':
        criterion = losses.create(args.loss, alpha=args.alpha, beta=args.beta).cuda()
    elif args.loss == 'neighbour':
        criterion = losses.create(args.loss, k=args.k, margin=args.margin).cuda()
    elif args.loss == 'nca':
        criterion = losses.create(args.loss, alpha=args.alpha, k=args.k).cuda()
    elif args.loss == 'triplet':
        criterion = losses.create(args.loss, alpha=args.alpha).cuda()
    elif args.loss in ['bin', 'ori_bin']:
        criterion = losses.create(args.loss, margin=args.margin, alpha=args.alpha, beta=args.beta)
    elif args.loss in ['hdc', 'hdc_bin']:
        criterion = losses.create(args.loss, margin=args.margin).cuda()
    else:
        criterion = losses.create(args.loss).cuda()

    # Decor_loss = losses.create('decor').cuda()
    data = DataSet.create(args.data, root=None)

    if args.data in ['shop', 'jd', 'cub']:
        train_loader = torch.utils.data.DataLoader(
            data.train, batch_size=args.BatchSize,
            sampler=FastRandomIdentitySampler(data.train, num_instances=args.num_instances),
            drop_last=True, pin_memory=True, num_workers=args.nThreads)
    else:
        train_loader = torch.utils.data.DataLoader(
            data.train, batch_size=args.BatchSize,
            sampler=RandomIdentitySampler(data.train, num_instances=args.num_instances),
            drop_last=True, pin_memory=True, num_workers=args.nThreads)

    # save the train information
    epoch_list = list()
    loss_list = list()
    pos_list = list()
    neg_list = list()

    for epoch in range(args.start, args.epochs):
        epoch_list.append(epoch)

        running_loss = 0.0
        running_pos = 0.0
        running_neg = 0.0

        if (epoch == 20 and args.data != 'shop') or (epoch == 2 and args.data in ['shop', 'jd']):
            optimizer.param_groups[0]['lr_mul'] = 0.1

        if (epoch == 1000 and args.data == 'car') or \
                (epoch == 550 and args.data == 'cub') or \
                (epoch == 100 and args.data in ['shop', 'jd']):

            param_groups = [
                {'params': base_params, 'lr_mult': 0.1},
                {'params': new_params, 'lr_mult': 1.0}]

            optimizer = torch.optim.Adam(param_groups, lr=0.1*args.lr,
                                         weight_decay=args.weight_decay)

        for i, data in enumerate(train_loader, 0):
            inputs, labels = data
            # wrap them in Variable
            inputs = Variable(inputs.cuda())

            # type of labels is Variable cuda.Longtensor
            labels = Variable(labels).cuda()

            optimizer.zero_grad()

            embed_feat = model(inputs)

            loss, inter_, dist_ap, dist_an = criterion(embed_feat, labels)

            # decor_loss = Decor_loss(embed_feat)

            # loss += args.theta * decor_loss

            if not type(loss) == torch.Tensor:
                continue

            loss.backward()

            optimizer.step()

            running_loss += loss.item()
            running_neg += dist_an
            running_pos += dist_ap

            if epoch == 0 and i == 0:
                print(50 * '#')
                print('Train Begin -- HA-HA-HA-HA-AH-AH-AH-AH --')

        loss_list.append(running_loss / (i+1))
        pos_list.append(running_pos / (i+1))
        neg_list.append(running_neg / (i+1))
        # print([type(z) for z in [epoch, running_loss[epoch], inter_, pos_list[epoch], neg_list[epoch]]])
        print('[Epoch %05d]\t Loss: %.3f \t Accuracy: %.3f \t Pos-Dist: %.3f \t Neg-Dist: %.3f'
              % (epoch, loss_list[epoch], inter_, pos_list[epoch], neg_list[epoch]))

        if epoch % args.save_step == 0:
            save_model(model, os.path.join(log_dir, '%d_model.pth' % epoch))
    np.savez(os.path.join(log_dir, "result.npz"), epoch=epoch_list, loss=loss_list, pos=pos_list, neg=neg_list)
    t = time.time() - s_
    print('training takes %.2f hour' % (t/3600))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Deep Metric Learning')

    # hype-parameters
    parser.add_argument('-lr', type=float, default=1e-5, help="learning rate of new parameters")
    parser.add_argument('-BatchSize', '-b', default=128, type=int, metavar='N',
                        help='mini-batch size (1 = pure stochastic) Default: 256')
    parser.add_argument('-num_instances', default=8, type=int, metavar='n',
                        help=' number of samples from one class in mini-batch')
    parser.add_argument('-dim', default=512, type=int, metavar='n',
                        help='dimension of embedding space')
    parser.add_argument('-alpha', default=30, type=int, metavar='n',
                        help='hyper parameter in NCA and its variants')
    parser.add_argument('-beta', default=0, type=float, metavar='n',
                        help='hyper parameter in some deep metric loss functions')
    # parser.add_argument('-theta', default=0.1, type=float,
    #                     help='hyper parameter coefficient for de-correlation loss')
    parser.add_argument('-k', default=16, type=int, metavar='n',
                        help='number of neighbour points in KNN')
    parser.add_argument('-margin', default=0.5, type=float,
                        help='margin in loss function')
    parser.add_argument('-init', default='random',
                        help='the initialization way of FC layer')
    parser.add_argument('-orth', default=0, type=float,
                        help='the coefficient orthogonal regularized term')

    # network
    parser.add_argument('-BN', default=1, type=int, required=True,metavar='N',
                        help='Freeze BN if 1')
    parser.add_argument('-data', default='cub', required=True,
                        help='path to Data Set')
    parser.add_argument('-net', default='vgg')
    parser.add_argument('-loss', default='branch', required=True,
                        help='loss for training network')
    parser.add_argument('-epochs', default=600, type=int, metavar='N',
                        help='epochs for training process')
    parser.add_argument('-save_step', default=50, type=int, metavar='N',
                        help='number of epochs to save model')

    # Resume from checkpoint
    parser.add_argument('-r', default=None,
                        help='the path of the pre-trained model')
    parser.add_argument('-start', default=0, type=int,
                        help='resume epoch')

    # basic parameter
    parser.add_argument('-checkpoints', default='/opt/intern/users/xunwang',
                        help='where the trained models save')
    parser.add_argument('-log_dir', default=None,
                        help='where the trained models save')
    parser.add_argument('--nThreads', '-j', default=18, type=int, metavar='N',
                        help='number of data loading threads (default: 2)')
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=2e-4)
    parser.add_argument('-step_1', type=int, default=250,
                        help='learn rate /10')

    main(parser.parse_args())



