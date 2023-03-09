#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @Time  : 2021/1/12 14:55
# @Author: yzf
import os
import sys
import argparse
import time
import random
import shutil
import torch
import logging
from pathlib import Path
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from utils import (
    get_fold_from_json, tup_to_dict, poly_lr, AvgMeter, expand_as_one_hot, compute_per_channel_dice,
    compute_dsc, bce2d_new, save_volume
)
from cacheio.Dataset import (
    Compose, PersistentDataset, LoadImage,
    Clip, ForeNormalize, RandFlip, RandRotate, ToTensor
)
from models.unet_nine_layers.unet_l9_deep_sup import UNetL9DeepSup

val_freq = 2

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=str, default='0')
parser.add_argument('--fold', type=int, default=0)
parser.add_argument('--size', type=tuple, default=(160, 160, 64))
parser.add_argument('--batch_size', type=int, default=2)
parser.add_argument('--net', type=str, default='unet_l9_ds')
parser.add_argument('--init_channels', type=int, default=16)
parser.add_argument('--optim', type=str, default='adam')
parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--N', type=int, default=-1)
parser.add_argument('--momentum', type=float, default=0.9)  # for SGD
parser.add_argument('--weight_decay', type=float, default=3e-4)
parser.add_argument('--num_class', type=int, default=17, help="1 background class and 16 forground classes")
parser.add_argument('--organs', type=list, default=
['liver', 'spleen', 'left_kidney', 'right_kidney', 'stomach', 'gallbladder', 'esophagus', 'pancreas',
 'duodenum', 'colon', 'intensine', 'adrenal', 'rectum', 'bladder', 'head_of_femur_l', 'head_of_femur_r'])
parser.add_argument('--num_epoch', type=int, default=400)
parser.add_argument('--seed', default=1234, type=int, help='seed for initializing training.')
parser.add_argument('--resume', default=False, action='store_true')
parser.add_argument('--beta', type=float, default=1.)  # for DSC
parser.add_argument('--beta2', type=float, default=1.)  # for edge
parser.add_argument('--cv_json', type=str, default='/data/yzf/dataset/organct/external/cross_validation.json')
parser.add_argument('--basepath', type=str, default='/data/yzf/dataset/organct/external/preprocessed')

def tr_summary(writer, epoch, c_lr, loss_seg, dsc, loss_edge=None):
    writer.add_scalar('tr_monitor/poly_lr', c_lr, epoch)
    writer.add_scalar('tr_monitor/loss_seg', loss_seg.avg, epoch)
    writer.add_scalar('tr_monitor/dsc', dsc.avg, epoch)
    if loss_edge is not None:
        writer.add_scalar('tr_monitor/loss_edge', loss_edge.avg, epoch)

def val_summary(writer, epoch, loss_seg, organs_dsc, avg_dsc, loss_edge=None):
    writer.add_scalar('val_monitor/loss_seg', loss_seg.avg, epoch)
    writer.add_scalar('val_monitor/avg_dsc', avg_dsc.avg, epoch)
    writer.add_scalar('val_dice/liver', organs_dsc['liver'].avg, epoch)
    writer.add_scalar('val_dice/spleen', organs_dsc['spleen'].avg, epoch)  # best sorted based on indices
    writer.add_scalar('val_dice/left_kidney', organs_dsc['left_kidney'].avg, epoch)
    writer.add_scalar('val_dice/right_kidney', organs_dsc['right_kidney'].avg, epoch)
    writer.add_scalar('val_dice/stomach', organs_dsc['stomach'].avg, epoch)
    writer.add_scalar('val_dice/gallbladder', organs_dsc['gallbladder'].avg, epoch)
    writer.add_scalar('val_dice/esophagus', organs_dsc['esophagus'].avg, epoch)
    writer.add_scalar('val_dice/pancreas', organs_dsc['pancreas'].avg, epoch)
    writer.add_scalar('val_dice/duodenum', organs_dsc['duodenum'].avg, epoch)
    writer.add_scalar('val_dice/colon', organs_dsc['colon'].avg, epoch)
    writer.add_scalar('val_dice/intensine', organs_dsc['intensine'].avg, epoch)
    writer.add_scalar('val_dice/adrenal', organs_dsc['adrenal'].avg, epoch)
    writer.add_scalar('val_dice/rectum', organs_dsc['rectum'].avg, epoch)
    writer.add_scalar('val_dice/bladder', organs_dsc['bladder'].avg, epoch)
    writer.add_scalar('val_dice/head_of_femur_l', organs_dsc['head_of_femur_l'].avg, epoch)
    writer.add_scalar('val_dice/head_of_femur_r', organs_dsc['head_of_femur_r'].avg, epoch)
    if loss_edge is not None:
        writer.add_scalar('val_monitor/loss_edge', loss_edge.avg, epoch)

def save_checkpoint(state, is_best, fd):
    filename = os.path.join(fd, 'checkpoint.pth.tar')
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, os.path.join(fd, 'model_best.pth.tar'))

def resume_model(model, fd):
    state = torch.load(os.path.join(fd, 'checkpoint.pth.tar'))
    latest_epoch = state['epoch']
    tol_time = state['tol_time']
    model.load_state_dict(state['state_dict'])
    return model, latest_epoch, tol_time

def get_model(args):
    model = None
    # UNet 9 layers
    if args.net == 'unet_l9_ds':
        model = UNetL9DeepSup(1, args.num_class, init_ch=args.init_channels)
    if model is None:
        raise ValueError('Model is None.')
    return model

# def _add_edge_files(files_list):
#     new_list = []
#     for i in files_list:
#         edge_file = i[0].replace('preproc_img', 'edge').replace('img', 'edge')
#         tup = (i[0], i[1], edge_file)
#         new_list.append(tup)
#     return new_list

def get_dataloader(args):
    train_list, val_list = get_fold_from_json(args.cv_json, args.fold)
    train_list = [(os.path.join(args.basepath, 'image', _), os.path.join(args.basepath, 'label', _),
                   os.path.join(args.basepath, 'edge', _)) for _ in train_list]
    val_list = [(os.path.join(args.basepath, 'image', _), os.path.join(args.basepath, 'label', _),
                   os.path.join(args.basepath, 'edge', _)) for _ in val_list]
    # dict
    d_train_list = tup_to_dict(train_list)
    d_val_list = tup_to_dict(val_list)

    train_transforms = Compose([LoadImage(keys=['image', 'label', 'edge']),
                                Clip(keys=['image'], min=-250., max=200.),
                                ForeNormalize(keys=['image'], mask_key='label'),
                                RandFlip(keys=['image', 'label', 'edge'], spatial_axis=(0, 1), prob=.5),
                                RandRotate(keys=['image', 'label', 'edge'], interp_order=[1, 0, 0], angle=15.0, prob=.5),
                                ToTensor(keys=['image', 'label', 'edge'])])

    val_transforms = Compose([LoadImage(keys=['image', 'label', 'edge']),
                              Clip(keys=['image'], min=-250., max=200.),
                              ForeNormalize(keys=['image'], mask_key='label'),
                              ToTensor(keys=['image', 'label', 'edge'])])

    # Regular Dataset
    # train_dataset = RegularDataset(data=d_train_list, transform=train_transforms)
    # val_dataset = RegularDataset(data=d_val_list, transform=val_transforms)

    # Persistent Dataset
    train_dataset = PersistentDataset(data=d_train_list, transform=train_transforms, cache_dir=args.cache_dir,)
    val_dataset = PersistentDataset(data=d_val_list, transform=val_transforms, cache_dir=args.cache_dir,)

    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=2, shuffle=True)
    val_dataloader = DataLoader(val_dataset, batch_size=1, num_workers=2, shuffle=False)
    return train_dataloader, val_dataloader

def parse_data(data):
    img_file = data['img_file']
    image = data['image']
    label = data['label']
    edge = data['edge']
    return img_file, image, label, edge

def adjust_lr(optimizer, epoch, max_epoch, initial_lr=1e-4, N=-1):
    """
    We use "poly" learning rate decay strategy and fix the learning rate at N epoch.

    Args:
        epoch: start from 0
        N: N < 0 means always adopting the decay strategy
    """
    # In main process, epoch starts from 1.
    epoch = epoch-1
    N = N-1

    new_lr = poly_lr(epoch, max_epoch, initial_lr)
    for param_group in optimizer.param_groups:
        param_group['lr'] = new_lr

    if epoch >= N and not N < 0:
        new_lr = poly_lr(N, max_epoch, initial_lr)  # fixed learning rate
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lr
    return param_group['lr']

def compute_loss(outputs, seg, seg_one_hot):
    loss_ce = torch.nn.CrossEntropyLoss()(outputs, seg.squeeze(1).long())
    output_soft = F.softmax(outputs, dim=1)
    seg_dice = compute_per_channel_dice(output_soft, seg_one_hot)
    loss_dice = 1. - seg_dice.mean()
    predicted_map = torch.argmax(output_soft, dim=1, keepdim=True).float()
    return loss_dice, loss_ce, seg_dice, predicted_map

def train_process(epoch, args, net, optimizer, train_dataloader, writer=None):
    """training w/o edge"""
    tr_start = time.time()  # timing
    c_lr = adjust_lr(optimizer, epoch=epoch, max_epoch=args.num_epoch, initial_lr=args.lr, N=args.N)
    net.train()

    loss_seg_meter = AvgMeter()
    dsc_meter = AvgMeter()

    len_train_batch = len(train_dataloader)
    for i, tr_data in enumerate(train_dataloader):
        case, volume, seg, _ = parse_data(tr_data)
        seg_one_hot = expand_as_one_hot(seg.squeeze(1).long(), args.num_class).cuda()
        volume = volume.cuda()
        seg = seg.cuda()

        outputs = net(volume)
        loss_dice, loss_ce, seg_dsc, predicted_map = compute_loss(outputs, seg, seg_one_hot)
        loss = loss_dice + args.beta * loss_ce

        loss_seg_meter.update((loss_dice + args.beta * loss_ce).item(), volume.shape[0])
        dsc_meter.update((seg_dsc[1:].mean()).item(), volume.shape[0])

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    logging.info('training epoch: {:d}, take: {:.4f} s, lr: {:.6f}, loss_seg: {:.4f}, tr_avg_dice: {:.4f}'
                 .format(epoch, time.time() - tr_start, c_lr, loss_seg_meter.avg, dsc_meter.avg))
    tr_summary(writer, epoch, c_lr, loss_seg_meter, dsc_meter)

def val_process(epoch, args, net, val_dataloader, writer=None):
    """validation w/o edge"""
    val_time = time.time()
    net.eval()
    # validation
    val_loss_seg_meter = AvgMeter()
    val_organs_dsc_meter = dict([(org, AvgMeter()) for org in args.organs])
    val_avg_dsc_meter = AvgMeter()
    len_val = len(val_dataloader)
    with torch.no_grad():
        for i, val_data in enumerate(val_dataloader):
            case, volume, seg, _ = parse_data(val_data)
            seg_one_hot = expand_as_one_hot(seg.squeeze(1).long(), args.num_class).cuda()
            volume = volume.cuda()
            seg = seg.cuda()

            outputs = net(volume)
            val_loss_dice, val_loss_ce, seg_dice, predicted_map = compute_loss(outputs, seg, seg_one_hot)
            organs_dsc = compute_dsc(predicted_map, seg, args.num_class)

            val_loss_seg_meter.update((val_loss_dice+args.beta*val_loss_ce).item(), volume.shape[0])
            for ind, key in enumerate(val_organs_dsc_meter.keys()):
                # if not math.isnan(organs_dsc[ind]):
                val_organs_dsc_meter[key].update(organs_dsc[ind].item(), volume.shape[0])
            val_avg_dsc_meter.update(organs_dsc.mean(), volume.shape[0])

    logging.info('validation epoch: {:d}, take: {:.4f} s, loss_seg: {:.4f}, val_avg_dsc: {:.4f}'
                 .format(epoch, time.time() - val_time, val_loss_seg_meter.avg, val_avg_dsc_meter.avg))
    val_summary(writer, epoch, val_loss_seg_meter, val_organs_dsc_meter, val_avg_dsc_meter)

    return val_avg_dsc_meter.avg

def main_worker(args):
    ## child process
    # tensorboard
    start_epoch = 1
    tol_time = 0.
    writer = None
    tbx_root = args.root + '/tbx'
    if not os.path.exists(tbx_root):
        os.makedirs(tbx_root)
    writer = SummaryWriter(log_dir=tbx_root)

    # model training
    best_dsc = .0
    model = get_model(args)
    if args.resume:
        model, latest_epoch, tol_time = resume_model(model, args.root)
        start_epoch = latest_epoch+1

    model.cuda()
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    train_dataloader, val_dataloader = get_dataloader(args)
    for epoch in range(start_epoch, args.num_epoch+1):
        tic = time.time()
        train_process(epoch, args, model, optimizer, train_dataloader, writer)
        if epoch % val_freq == 0:
            dsc = val_process(epoch, args, model, val_dataloader, writer)

            # remember best dsc and save checkpoint
            is_best = dsc > best_dsc
            best_dsc = max(dsc, best_dsc)
            if is_best:
                with open(args.root + '/record_best.txt', 'w') as f:
                    f.write('epoch {0}: {1:.4f}'.format(epoch, best_dsc))
            save_checkpoint(
                {
                    'epoch': epoch,
                    'state_dict': model.state_dict(),
                    'best_dsc': best_dsc,
                    'tol_time': tol_time,
                },
                is_best,
                args.root)

        tol_time += time.time()-tic
        writer.add_scalar('timing', tol_time/3600., epoch,)

    writer.close()

def main():
    args = parser.parse_args()
    args.cache_dir = f'./cache/external/{Path(__file__).stem}_{args.net}_fold{args.fold}'
    args.root = f'./outputs/external/{args.net}_fold{args.fold}_{time.strftime("%H-%M-%S-%m%d")}'

    # remove files
    root = Path(args.root)
    cache = Path(args.cache_dir)
    if not args.resume:
        if cache.exists():
            shutil.rmtree(cache)  # clear cache
        root_ = root / 'predictions/edge'
        root_.mkdir(parents=True)
        if not root_.is_dir():
            raise ValueError("root must be a directory.")

    # gpu
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    # deterministic training for reproducibility
    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True

    # logger
    logging.basicConfig(filename=args.root+"/log.txt", level=logging.INFO,
                        format='[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%H:%M:%S')
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    # log configurations
    with open(root / 'parameters.txt', 'w') as f:
        for a in vars(args).items():
            f.write(str(a)+'\n')

    # main process
    main_worker(args)

if __name__ == '__main__':
    main()