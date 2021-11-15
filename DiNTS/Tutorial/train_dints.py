# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import copy
import json
import logging
import monai
import nibabel as nib
import numpy as np
import os
import pandas as pd
import pathlib
import shutil
import sys
import tempfile
import time
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import yaml

from dints import DiNTS
from datetime import datetime
from glob import glob
from torch import nn
from torch.nn.parallel import DistributedDataParallel
from monai.data import (
    DataLoader,
    ThreadDataLoader,
    decollate_batch,
)
# from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from monai.transforms import (
    apply_transform,
    Randomizable,
    Transform,
    AsDiscrete,
    AsDiscreted,
    AddChannel,
    AddChanneld,
    AsChannelFirstd,
    CastToTyped,
    Compose,
    ConcatItemsd,
    CopyItemsd,
    CropForegroundd,
    DivisiblePadd,
    EnsureChannelFirstd,
    EnsureTyped,
    KeepLargestConnectedComponent,
    Lambdad,
    LoadImaged,
    NormalizeIntensityd,
    Orientationd,
    ScaleIntensityRanged,
    ThresholdIntensityd,
    RandCropByLabelClassesd,
    RandCropByPosNegLabeld,
    RandGaussianNoised,
    RandGaussianSmoothd,
    RandShiftIntensityd,
    RandScaleIntensityd,
    RandSpatialCropd,
    RandSpatialCropSamplesd,
    RandFlipd,
    RandRotated,
    RandRotate90d,
    RandZoomd,
    Spacingd,
    SpatialPadd,
    SqueezeDimd,
    ToDeviced,
    ToNumpyd,
    ToTensord,
)
from monai.data import Dataset, create_test_image_3d, DistributedSampler, list_data_collate, partition_dataset
from monai.inferers import sliding_window_inference
# from monai.losses import DiceLoss, FocalLoss, GeneralizedDiceLoss
from monai.metrics import compute_meandice
from monai.utils import set_determinism


def main():
    parser = argparse.ArgumentParser(description="training")
    parser.add_argument(
        "--arch_ckpt",
        action="store",
        required=True,
        help="data root",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="checkpoint full path",
    )
    parser.add_argument(
        "--fold",
        action="store",
        required=True,
        help="fold index in N-fold cross-validation",
    )
    parser.add_argument(
        "--json",
        action="store",
        required=True,
        help="full path of .json file",
    )
    parser.add_argument(
        "--json_key",
        action="store",
        required=True,
        help="selected key in .json data list",
    )
    parser.add_argument(
        "--local_rank",
        required=int,
        help="local process rank",
    )
    parser.add_argument(
        "--num_folds",
        action="store",
        required=True,
        help="number of folds in cross-validation",
    )
    parser.add_argument(
        "--output_root",
        action="store",
        required=True,
        help="output root",
    )
    parser.add_argument(
        "--root",
        action="store",
        required=True,
        help="data root",
    )
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    if not os.path.exists(args.output_root):
        os.makedirs(args.output_root)

    amp = True
    determ = False
    fold = int(args.fold)
    input_channels = 1
    learning_rate = 0.0001
    learning_rate_gamma = 1.0
    learning_rate_step_size = 1000
    num_images_per_batch = 2
    num_epochs = 1000
    num_epochs_per_validation = 20
    num_folds = int(args.num_folds)
    num_patches_per_image = 1
    num_sw_batch_size = 6
    output_classes = 3
    overlap_ratio = 0.625
    patch_size = (96, 96, 96)
    patch_size_valid = (96, 96, 96)
    spacing = [1.0, 1.0, 1.0]

    # deterministic training
    if determ:
        set_determinism(seed=0)

    # initialize the distributed training process, every GPU runs in a process
    dist.init_process_group(backend="nccl", init_method="env://")

    # data
    with open(args.json, "r") as f:
        json_data = json.load(f)

    split = len(json_data[args.json_key]) // num_folds
    list_train = json_data[args.json_key][:(split * fold)] + json_data[args.json_key][(split * (fold + 1)):]
    list_valid = json_data[args.json_key][(split * fold):(split * (fold + 1))]

    # training data
    files = []
    for _i in range(len(list_train)):
        str_img = os.path.join(args.root, list_train[_i]["image"])
        str_seg = os.path.join(args.root, list_train[_i]["label"])

        if (not os.path.exists(str_img)) or (not os.path.exists(str_seg)):
            continue

        files.append({"image": str_img, "label": str_seg})
    
    train_files = files
    train_files = partition_dataset(data=train_files, shuffle=True, num_partitions=dist.get_world_size(), even_divisible=True)[dist.get_rank()]
    print("train_files:", len(train_files))

    # validation data
    files = []
    for _i in range(len(list_valid)):
        str_img = os.path.join(args.root, list_valid[_i]["image"])
        str_seg = os.path.join(args.root, list_valid[_i]["label"])
                
        if (not os.path.exists(str_img)) or (not os.path.exists(str_seg)):
            continue

        files.append({"image": str_img, "label": str_seg})
    val_files = files
    val_files = partition_dataset(data=val_files, shuffle=False, num_partitions=dist.get_world_size(), even_divisible=False)[dist.get_rank()]
    print("val_files:", len(val_files))

    # network architecture
    device = torch.device(f"cuda:{args.local_rank}")
    torch.cuda.set_device(device)

    train_transforms = Compose(
        [
            LoadImaged(keys=["image", "label"]),
            EnsureChannelFirstd(keys=["image", "label"]),
            Orientationd(keys=["image", "label"], axcodes="RAS"),
            Spacingd(keys=["image", "label"], pixdim=spacing, mode=("bilinear", "nearest"), align_corners=(True, True)),
            CastToTyped(keys=["image"], dtype=(torch.float32)),
            ScaleIntensityRanged(keys=["image"], a_min=-125.0, a_max=275.0, b_min=0.0, b_max=1.0, clip=True),
            CastToTyped(keys=["image", "label"], dtype=(np.float16, np.uint8)),
            CopyItemsd(keys=["label"], times=1, names=["label4crop"]),
            Lambdad(
                keys=["label4crop"],
                func=lambda x: np.concatenate(tuple([ndimage.binary_dilation((x==_k).astype(x.dtype), iterations=48).astype(x.dtype) for _k in range(output_classes)]), axis=0),
                overwrite=True,
            ),
            EnsureTyped(keys=["image", "label"]),
            CastToTyped(keys=["image"], dtype=(torch.float32)),
            SpatialPadd(keys=["image", "label", "label4crop"], spatial_size=patch_size, mode=["reflect", "constant", "constant"]),
            RandCropByLabelClassesd(
                keys=["image", "label"],
                label_key="label4crop",
                num_classes=output_classes,
                ratios=[1,] * output_classes,
                spatial_size=patch_size,
                num_samples=num_patches_per_image
            ),
            Lambdad(keys=["label4crop"], func=lambda x: 0),
            RandRotated(keys=["image", "label"], range_x=0.3, range_y=0.3, range_z=0.3, mode=["bilinear", "nearest"], prob=0.2),
            RandZoomd(keys=["image", "label"],min_zoom=0.8,max_zoom=1.2,mode=["trilinear", "nearest"], align_corners=[True, None], prob=0.16),
            RandGaussianSmoothd(keys=["image"], sigma_x=(0.5,1.15), sigma_y=(0.5,1.15), sigma_z=(0.5,1.15), prob=0.15),
            RandScaleIntensityd(keys=["image"], factors=0.3, prob=0.5),
            RandShiftIntensityd(keys=["image"], offsets=0.1, prob=0.5),
            RandGaussianNoised(keys=["image"], std=0.01, prob=0.15),
            RandFlipd(keys=["image", "label"], spatial_axis=0, prob=0.5),
            RandFlipd(keys=["image", "label"], spatial_axis=1, prob=0.5),
            RandFlipd(keys=["image", "label"], spatial_axis=2, prob=0.5),
            CastToTyped(keys=["image", "label"], dtype=(torch.float32, torch.uint8)),
            ToTensord(keys=["image", "label"]),
        ]
    )

    val_transforms = Compose(
        [
            LoadImaged(keys=["image", "label"]),
            EnsureChannelFirstd(keys=["image", "label"]),
            Orientationd(keys=["image", "label"], axcodes="RAS"),
            Spacingd(keys=["image", "label"], pixdim=spacing, mode=("bilinear", "nearest"), align_corners=(True, True)),
            CastToTyped(keys=["image"], dtype=(torch.float32)),
            ScaleIntensityRanged(keys=["image"], a_min=-125.0, a_max=275.0, b_min=0.0, b_max=1.0, clip=True),
            CastToTyped(keys=["image", "label"], dtype=(np.float32, np.uint8)),
            EnsureTyped(keys=["image", "label"]),
            ToTensord(keys=["image", "label"])
        ]
    )

    train_ds = monai.data.CacheDataset(data=train_files, transform=train_transforms, cache_rate=1.0, num_workers=8)
    val_ds = monai.data.CacheDataset(data=val_files, transform=val_transforms, cache_rate=1.0, num_workers=2)

    train_loader = DataLoader(train_ds, batch_size=num_images_per_batch, shuffle=True, num_workers=8, pin_memory=torch.cuda.is_available())
    val_loader = DataLoader(val_ds, batch_size=1, shuffle=False, num_workers=2, pin_memory=torch.cuda.is_available())
    
    ckpt = torch.load(args.arch_ckpt)
    node_a = ckpt['node_a']
    code_a = ckpt['code_a']
    code_c = ckpt['code_c']

    model = DiNTS(
        in_channels=input_channels,
        num_classes=output_classes,
        cell_ops=5,
        num_blocks=12,
        num_depths=4,
        channel_mul=1.0,
        use_stem=True,
        code=[node_a, code_a, code_c]
    )
    
    code_a = torch.from_numpy(code_a).to(torch.float32).cuda()
    code_c = F.one_hot(torch.from_numpy(code_c), model.cell_ops).to(torch.float32).cuda()
    model = model.to(device)

    model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    post_pred = Compose([EnsureType(), AsDiscrete(argmax=True, to_onehot=True, num_classes=output_classes)])
    post_label = Compose([EnsureType(), AsDiscrete(to_onehot=True, num_classes=output_classes)])

    # loss function
    loss_func = monai.losses.DiceCELoss(
        include_background=False,
        to_onehot_y=True,
        softmax=True,
        squared_pred=True,
        batch=True,
        smooth_nr=0.00001,
        smooth_dr=0.00001,
    )

    # optimizer
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=learning_rate
    )

    print()

    if torch.cuda.device_count() > 1:
        if dist.get_rank() == 0:
            print("Let's use", torch.cuda.device_count(), "GPUs!")

        model = DistributedDataParallel(model, device_ids=[device], find_unused_parameters=True)

    if args.checkpoint != None and os.path.isfile(args.checkpoint):
        print("[info] fine-tuning pre-trained checkpoint {0:s}".format(args.checkpoint))
        model.load_state_dict(torch.load(args.checkpoint, map_location=device))
        torch.cuda.empty_cache()
    else:
        print("[info] training from scratch")

    # amp
    if amp:
        from torch.cuda.amp import autocast, GradScaler
        scaler = GradScaler()
        if dist.get_rank() == 0:
            print("[info] amp enabled")

    # start a typical PyTorch training
    val_interval = num_epochs_per_validation
    best_metric = -1
    best_metric_epoch = -1
    epoch_loss_values = list()
    idx_iter = 0
    metric_values = list()

    if dist.get_rank() == 0:
        print("num_tta", num_tta)
        print("flip_tta", flip_tta)

    if dist.get_rank() == 0:
        writer = SummaryWriter(log_dir=os.path.join(args.output_root, "Events"))

        with open(os.path.join(args.output_root, "accuracy_history.csv"), "a") as f:
            f.write("epoch\tmetric\tloss\tlr\ttime\titer\n")

    start_time = time.time()
    for epoch in range(num_epochs // num_epochs_per_validation):
        # if learning_rate_final > -0.000001 and learning_rate_final < learning_rate:
        #     # lr = learning_rate - epoch / (num_epochs - 1) * (learning_rate - learning_rate_final)
        #     lr = (learning_rate - learning_rate_final) * (1 - epoch / (num_epochs - 1)) ** 0.9 + learning_rate_final
        #     for param_group in optimizer.param_groups:
        #         param_group["lr"] = lr
        # else:
        #     lr = learning_rate
        
        # lr = learning_rate * (learning_rate_gamma ** (epoch // learning_rate_step_size))
        # for param_group in optimizer.param_groups:
        #     param_group["lr"] = lr

        lr = optimizer.param_groups[0]["lr"]

        if dist.get_rank() == 0:
            print("-" * 10)
            print(f"epoch {epoch * num_epochs_per_validation + 1}/{num_epochs}")
            print('learning rate is set to {}'.format(lr))

        model.train()
        epoch_loss = 0
        loss_torch = torch.zeros(2, dtype=torch.float, device=device)
        step = 0
        # train_sampler.set_epoch(epoch)
        for batch_data in train_loader:
            step += 1
            inputs, labels = batch_data["image"].to(device), batch_data["label"].to(device)

            # optimizer.zero_grad()
            for param in model.parameters():
                param.grad = None

            if amp:
                with autocast():
                    outputs = model(inputs, [node_a, code_a, code_c])
                    if output_classes == 2:
                        loss = loss_func(torch.flip(outputs[-1], dims=[1]), 1 - labels)
                    else:
                        loss = loss_func(outputs[-1], labels)

                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                outputs = model(inputs, [node_a, code_a, code_c])
                if output_classes == 2:
                    loss = loss_func(torch.flip(outputs[-1], dims=[1]), 1 - labels)
                else:
                    loss = loss_func(outputs[-1], labels)
                loss.backward()
                optimizer.step()

            epoch_loss += loss.item()
            loss_torch[0] += loss.item()
            loss_torch[1] += 1.0
            epoch_len = len(train_loader)
            idx_iter += 1

            if dist.get_rank() == 0:
                print("[{0}] ".format(str(datetime.now())[:19]) + f"{step}/{epoch_len}, train_loss: {loss.item():.4f}")
                writer.add_scalar("train_loss", loss.item(), epoch_len * epoch + step)

        # synchronizes all processes and reduce results
        dist.barrier()
        dist.all_reduce(loss_torch, op=torch.distributed.ReduceOp.SUM)
        loss_torch = loss_torch.tolist()
        if dist.get_rank() == 0:
            loss_torch_epoch = loss_torch[0] / loss_torch[1]
            print(f"epoch {(epoch + 1) * num_epochs_per_validation} average loss: {loss_torch_epoch:.4f}, best mean dice: {best_metric:.4f} at epoch {best_metric_epoch}")

        # if (epoch + 1) % val_interval == 0:
        if True:
            torch.cuda.empty_cache()
            model.eval()
            with torch.no_grad():
                metric = torch.zeros((output_classes - 1) * 2, dtype=torch.float, device=device)
                metric_sum = 0.0
                metric_count = 0
                metric_mat = []
                val_images = None
                val_labels = None
                val_outputs = None

                _index = 0
                for val_data in val_loader:
                    val_images = val_data["image"].to(device)
                    val_labels = val_data["label"].to(device)

                    roi_size = patch_size_valid
                    sw_batch_size = num_sw_batch_size

                    # test time augmentation
                    ct = 1.0
                    with torch.cuda.amp.autocast():
                        pred = sliding_window_inference(
                            val_images,
                            roi_size,
                            sw_batch_size,
                            lambda x: model(x, [node_a, code_a, code_c])[-1],
                            mode="gaussian",
                            overlap=overlap_ratio,
                        )

                    val_outputs = pred / ct

                    val_outputs = post_pred(val_outputs[0, ...])
                    val_outputs = val_outputs[None, ...]
                    val_labels = post_label(val_labels[0, ...])
                    val_labels = val_labels[None, ...]
                    # val_outputs = post_processing(val_outputs)

                    value = compute_meandice(
                                y_pred=val_outputs,
                                y=val_labels,
                                include_background=False
                            )

                    print(_index + 1, "/", len(val_loader), value)
                    
                    metric_count += len(value)
                    metric_sum += value.sum().item()
                    metric_vals = value.cpu().numpy()
                    if len(metric_mat) == 0:
                        metric_mat = metric_vals
                    else:
                        metric_mat = np.concatenate((metric_mat, metric_vals), axis=0)

                    for _c in range(output_classes - 1):
                        val0 = torch.nan_to_num(value[0, _c], nan=0.0)
                        val1 = 1.0 - torch.isnan(value[0, 0]).float()
                        metric[2 * _c] += val0 * val1
                        metric[2 * _c + 1] += val1

                    _index += 1

                # synchronizes all processes and reduce results
                dist.barrier()
                dist.all_reduce(metric, op=torch.distributed.ReduceOp.SUM)
                metric = metric.tolist()
                if dist.get_rank() == 0:
                    for _c in range(output_classes - 1):
                        print("evaluation metric - class {0:d}:".format(_c + 1), metric[2 * _c] / metric[2 * _c + 1])
                    avg_metric = 0
                    for _c in range(output_classes - 1):
                        avg_metric += metric[2 * _c] / metric[2 * _c + 1]
                    avg_metric = avg_metric / float(output_classes - 1)
                    print("avg_metric", avg_metric)

                    if avg_metric > best_metric:
                        best_metric = avg_metric
                        best_metric_epoch = epoch + 1
                        torch.save(model.state_dict(), os.path.join(args.output_root, "best_metric_model.pth"))
                        print("saved new best metric model")

                        dict_file = {}
                        dict_file["best_avg_dice_score"] = float(best_metric)
                        dict_file["best_avg_dice_score_epoch"] = int(best_metric_epoch)
                        dict_file["best_avg_dice_score_iteration"] = int(idx_iter)
                        with open(os.path.join(args.output_root, "progress.yaml"), "w") as out_file:
                            documents = yaml.dump(dict_file, stream=out_file)

                    print(
                        "current epoch: {} current mean dice: {:.4f} best mean dice: {:.4f} at epoch {}".format(
                            (epoch + 1) * num_epochs_per_validation, avg_metric, best_metric, best_metric_epoch
                        )
                    )

                    current_time = time.time()
                    elapsed_time = (current_time - start_time) / 60.0
                    with open(os.path.join(args.output_root, "accuracy_history.csv"), "a") as f:
                        f.write("{0:d}\t{1:.5f}\t{2:.5f}\t{3:.5f}\t{4:.1f}\t{5:d}\n".format((epoch + 1) * num_epochs_per_validation, avg_metric, loss_torch_epoch, lr, elapsed_time, idx_iter))

                dist.barrier()

            torch.cuda.empty_cache()

    print(f"train completed, best_metric: {best_metric:.4f} at epoch: {best_metric_epoch}")

    if dist.get_rank() == 0:
        writer.close()

    dist.destroy_process_group()

    return


if __name__ == "__main__":
    main()