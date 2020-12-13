import os
import random
import syft as sy
import pandas as pd
import numpy as np
from PIL import Image
from tqdm import tqdm
import torch
from torch import (  # pylint:disable=no-name-in-module
    manual_seed,
    stack,
    cat,
    std_mean,
    save,
    is_tensor,
    from_numpy,
    randperm,
    default_generator,
    tensor,
)
from torch._utils import _accumulate
import albumentations as a
from copy import deepcopy
from torch.utils import data as torchdata
from torchvision.datasets import MNIST
from torchvision import transforms
from torchvision.datasets.folder import default_loader

from os.path import splitext
from typing import Dict, Union, Set, Callable

from pathlib import Path
from .dicomtools import DicomLoader

from sklearn.model_selection import LeaveOneOut

from typing import Callable


class AlbumentationsTorchTransform:
    def __init__(self, transform, **kwargs):
        # print("init albu transform wrapper")
        self.transform = transform
        self.kwargs = kwargs

    def __call__(self, img):
        # print("call albu transform wrapper")
        if Image.isImageType(img):
            img = np.array(img)
        elif is_tensor(img):
            img = img.cpu().numpy()
        img = self.transform(image=img, **self.kwargs)["image"]
        # if img.max() > 1:
        #     img = a.augmentations.functional.to_float(img, max_value=255)
        img = from_numpy(img)
        if img.shape[-1] < img.shape[0]:
            img = img.permute(2, 0, 1)
        return img


class CombinedLoader:
    """Class that combines several data loaders and their extensions.

    Args:
        mapping (Dict): Dictionary that maps loader names to tuples
                        consisting of (corresponding extensions, loader method)
    """

    def __init__(
        self,
        mapping: Dict[str, Dict[str, Union[Set[str], Callable]]] = {
            "default": {
                "extensions": {
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".ppm",
                    ".bmp",
                    ".pgm",
                    ".tif",
                    ".tiff",
                    ".webp",
                },
                "loader": default_loader,
            },
            "dicom": {"extensions": {".dcm", ".dicom"}, "loader": DicomLoader(3)},
        },
    ):
        self.extensions = set()
        self.mapping = mapping
        self.ext_to_loader_name = dict()
        for loader_name, defining_dict in mapping.items():
            self.extensions |= defining_dict["extensions"]
            for ext in defining_dict["extensions"]:
                if ext in self.ext_to_loader_name:
                    raise RuntimeError(
                        "Extension {:s} was passed for multiple loaders".format(ext)
                    )
                self.ext_to_loader_name[ext] = loader_name

    def __call__(self, path: Path, **kwargs):
        """Apply loader to path

        Args:
            path (Path): path to file.
            kwargs: kwargs passed to load methods

        Returns:
            Image: a PIL image of the given path

        Raises:
            RuntimeError: If loader for path extension not specified.
        """
        file_ending = splitext(path)[1].lower()
        if file_ending in self.extensions:
            return self.mapping[self.ext_to_loader_name[file_ending]]["loader"](
                path, **kwargs
            )
        else:
            raise RuntimeError(
                "file extension does not match specified supported extensions. "
                "Please provide the matching loader for the {:s} extension.".format(
                    file_ending
                )
            )

    def change_channels(self, num_channels: int):
        """Change the number of channels that are loaded (Default: 3)

        Args:
            num_channels (int): Number of channels. Currently only 1 and 3 supported

        Raises:
            RuntimeError: if num_channels is not 1 or 3
        """
        if num_channels not in [1, 3]:
            raise RuntimeError("Only 1 or 3 channels supported yet.")
        self.mapping["default"]["loader"] = (
            single_channel_loader if num_channels == 1 else default_loader
        )
        self.mapping["dicom"]["loader"] = DicomLoader(num_channels)


def create_albu_transform(args, mean, std):
    train_tf = transforms.RandomAffine(
        degrees=args.rotation,
        translate=(args.translate, args.translate),
        scale=(1.0 - args.scale, 1.0 + args.scale),
        shear=args.shear,
        #    fillcolor=0,
    )
    start_transformations = [
        a.Resize(args.inference_resolution, args.inference_resolution),
        a.RandomCrop(args.train_resolution, args.train_resolution),
    ]
    if args.clahe:
        start_transformations.extend(
            [
                a.FromFloat(dtype="uint8", max_value=1.0),
                a.CLAHE(always_apply=True, clip_limit=(1, 1)),
            ]
        )
    train_tf_albu = [
        a.VerticalFlip(p=args.individual_albu_probs),
    ]
    if args.randomgamma:
        train_tf_albu.append(a.RandomGamma(p=args.individual_albu_probs))
    if args.randombrightness:
        train_tf_albu.append(a.RandomBrightness(p=args.individual_albu_probs))
    if args.blur:
        train_tf_albu.append(a.Blur(p=args.individual_albu_probs))
    if args.elastic:
        train_tf_albu.append(a.ElasticTransform(p=args.individual_albu_probs))
    if args.optical_distortion:
        train_tf_albu.append(a.OpticalDistortion(p=args.individual_albu_probs))
    if args.grid_distortion:
        train_tf_albu.append(a.GridDistortion(p=args.individual_albu_probs))
    if args.grid_shuffle:
        train_tf_albu.append(a.RandomGridShuffle(p=args.individual_albu_probs))
    if args.hsv:
        train_tf_albu.append(a.HueSaturationValue(p=args.individual_albu_probs))
    if args.invert:
        train_tf_albu.append(a.InvertImg(p=args.individual_albu_probs))
    if args.cutout:
        train_tf_albu.append(
            a.Cutout(
                num_holes=5, max_h_size=80, max_w_size=80, p=args.individual_albu_probs
            )
        )
    if args.shadow:
        assert args.pretrained, "RandomShadows needs 3 channels"
        train_tf_albu.append(a.RandomShadow(p=args.individual_albu_probs))
    if args.fog:
        assert args.pretrained, "RandomFog needs 3 channels"
        train_tf_albu.append(a.RandomFog(p=args.individual_albu_probs))
    if args.sun_flare:
        assert args.pretrained, "RandomSunFlare needs 3 channels"
        train_tf_albu.append(a.RandomSunFlare(p=args.individual_albu_probs))
    if args.solarize:
        train_tf_albu.append(a.Solarize(p=args.individual_albu_probs))
    if args.equalize:
        train_tf_albu.append(a.Equalize(p=args.individual_albu_probs))
    if args.grid_dropout:
        train_tf_albu.append(a.GridDropout(p=args.individual_albu_probs))
    train_tf_albu.append(a.GaussNoise(var_limit=args.noise_std ** 2, p=args.noise_prob))
    end_transformations = [
        a.ToFloat(max_value=255.0),
        a.Normalize(mean, std, max_pixel_value=1.0),
    ]
    if not args.pretrained:
        end_transformations.append(
            a.Lambda(image=lambda x, **kwargs: x[:, :, np.newaxis])
        )
    train_tf_albu = AlbumentationsTorchTransform(
        a.Compose(
            [
                a.Compose(start_transformations),
                a.Compose(train_tf_albu, p=args.albu_prob),
                a.Compose(end_transformations),
            ]
        )
    )
    return transforms.Compose(
        [
            train_tf,
            train_tf_albu,
        ]
    )


def l1_sensitivity(query: Callable, d: tensor) -> float:
    """Calculates L1-sensitivity of a query on a dataset."""
    L = LeaveOneOut()
    data = d.copy()
    sensitivity = 0
    for idx in L.split(data):
        val = query(data[idx[0]])
        if val > sensitivity:
            sensitivity = val
    return sensitivity


def calc_mean_std(
    dataset,
    save_folder=None,
    epsilon=None,
):
    """
    Calculates the mean and standard deviation of `dataset` and
    saves them to `save_folder`.

    Needs a dataset where all images have the same size.

    If epsilon is provided, does so in a differentially private way.
    """

    accumulated_data = []
    for d in tqdm(
        dataset, total=len(dataset), leave=False, desc="accumulate data in dataset"
    ):
        if type(d) is tuple or type(d) is list:
            d = d[0]
        accumulated_data.append(d)
    if isinstance(dataset, torchdata.Dataset):
        accumulated_data = stack(accumulated_data)
    elif isinstance(dataset, torchdata.DataLoader):
        accumulated_data = cat(accumulated_data)
    else:
        raise NotImplementedError("don't know how to process this data input class")
    if accumulated_data.shape[1] in [1, 3]:  # ugly hack
        dims = (0, *range(2, len(accumulated_data.shape)))
    else:
        dims = (*range(len(accumulated_data.shape)),)
    if epsilon:
        mean_sens = l1_sensitivity(torch.mean, accumulated_data)
        std_sens = l1_sensitivity(torch.std, accumulated_data)
        std, mean = std_mean(accumulated_data, dim=dims)
        std += torch.distributions.laplace.Laplace(
            loc=0, scale=std_sens / epsilon
        ).rsample()
        mean += torch.distributions.laplace.Laplace(
            loc=0, scale=mean_sens / epsilon
        ).rsample()
    else:
        std, mean = std_mean(accumulated_data, dim=dims)
    if save_folder:
        save(stack([mean, std]), os.path.join(save_folder, "mean_std.pt"))
    return mean, std


def single_channel_loader(filename):
    """Converts `filename` to a grayscale PIL Image"""
    with open(filename, "rb") as f:
        img = Image.open(f).convert("L")
        return img.copy()


class LabelMNIST(MNIST):
    def __init__(self, labels, *args, **kwargs):
        super().__init__(*args, **kwargs)
        indices = np.isin(self.targets, labels).astype("bool")
        self.data = self.data[indices]
        self.targets = self.targets[indices]


class PathDataset(torchdata.Dataset):
    def __init__(
        self,
        root,
        transform=None,
        loader=CombinedLoader(),
        extensions=[
            ".jpg",
            ".jpeg",
            ".png",
            ".ppm",
            ".bmp",
            ".pgm",
            ".tif",
            ".tiff",
            ".webp",
            ".dcm",
            ".dicom",
        ],
    ):
        super(PathDataset, self).__init__()
        self.root = root
        self.transform = transform
        self.loader = loader
        self.imgs = [
            f
            for f in os.listdir(root)
            if os.path.splitext(f)[1].lower() in extensions
            and not os.path.split(f)[1].lower().startswith("._")
        ]

    def __len__(self):
        return len(self.imgs)

    def __getitem__(self, idx):
        img_path = self.imgs[idx]
        img = self.loader(os.path.join(self.root, img_path))
        if self.transform:
            img = self.transform(img)
        return img


class RemoteTensorDataset(torchdata.Dataset):
    def __init__(self, tensor):
        self.tensor = tensor

    def __len__(self):
        return self.tensor.shape[0]

    def __getitem__(self, idx):
        return self.tensor[idx].copy()


class ImageFolderFromCSV(torchdata.Dataset):
    def __init__(
        self, csv_path, img_folder_path, transform=None, target_transform=None
    ):
        super().__init__()
        self.transform = transform
        self.target_transform = target_transform
        self.img_folder_path = img_folder_path
        self.img_files = [
            i for i in os.listdir(img_folder_path) if not i.startswith(".")
        ]

        metastats = pd.read_csv(csv_path)

        metastats["class_label"] = metastats.apply(
            ImageFolderFromCSV.__meta_to_class__, axis=1
        )
        self.categorize_dict = dict(
            zip(metastats.X_ray_image_name, metastats.class_label)
        )
        for img in self.img_files:
            assert (
                img in self.categorize_dict.keys()
            ), "img label not known {:s}".format(str(img))
            if self.categorize_dict[img] == -1:
                self.img_files.remove(img)
                print("Ignore image {:s} because category is certain".format(img))

    @staticmethod
    def __meta_to_class__(row):
        if row["Label"] == "Normal":
            return 0
        if row["Label"] == "Pnemonia":  # i know this is a typo but was in original csv
            if row["Label_1_Virus_category"] == "bacteria":
                return 1
            if row["Label_1_Virus_category"] == "Virus":
                return 2
        return -1

    def __getitem__(self, i):
        img_path = self.img_files[i]
        label = self.categorize_dict[img_path]
        img = single_channel_loader(os.path.join(self.img_folder_path, img_path))
        if self.transform:
            img = self.transform(img)
        if self.target_transform:
            label = self.target_transform(label)
        return img, label

    def __len__(self):
        return len(self.img_files)


class PPPP(torchdata.Dataset):
    def __init__(
        self,
        label_path="data/Labels.csv",
        train=False,
        transform=None,
        seed=1,
    ):
        super().__init__()
        random.seed(seed)
        manual_seed(seed)
        self.train = train
        self.labels = pd.read_csv(label_path)
        self.labels = self.labels[
            self.labels["Dataset_type"] == ("TRAIN" if train else "TEST")
        ]
        self.transform = transform
        """
        Split into train and validation set
        if self.train:
            indices = [
                i
                for i in range(len(self.labels))
                if ((i % self.val_split) != 0 and self.val)
                or (not self.val and (i % self.val_split) == 0)
            ]
            self.labels = self.labels.drop(index=indices)
        """

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        row = self.labels.iloc[index]
        label = row["Numeric_Label"]
        path = "train" if self.train else "test"
        path = os.path.join("data", path, row["X_ray_image_name"])
        img = single_channel_loader(path)
        if self.transform:
            img = self.transform(img)
        return img, label

    # def get_class_name(self, numeric_label):
    #    return self.class_names[numeric_label]

    """
    Works only if not torch.utils.torchdata.random_split is applied
    """

    def get_class_occurances(self):
        return dict(self.labels["Numeric_Label"].value_counts())

    def __compute_mean_std__(self):

        calc_mean_std(
            self,
            save_folder="data",
        )


##This is from torch.data.utils and adapted for our purposes
class Subset(torchdata.Dataset):
    def __init__(self, dataset, indices):
        self.dataset = deepcopy(dataset)
        self.indices = indices

    def __getitem__(self, idx):
        return self.dataset[self.indices[idx]]

    def __len__(self):
        return len(self.indices)


def random_split(dataset, lengths, generator=default_generator):
    if sum(lengths) != len(dataset):
        raise ValueError(
            "Sum of input lengths does not equal the length of the input dataset!"
        )

    indices = randperm(sum(lengths), generator=generator).tolist()
    return [
        Subset(dataset, indices[offset - length : offset])
        for offset, length in zip(_accumulate(lengths), lengths)
    ]


if __name__ == "__main__":
    # import matplotlib.pyplot as plt
    import sys
    from tqdm import tqdm
    import numpy as np

    sys.path.append(
        os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
    )
    from torchlib.utils import AddGaussianNoise

    ds = PPPP(train=True, transform=transforms.ToTensor())
    print("Class distribution")
    print(ds.get_class_occurances())

    sizes = []

    for data, _ in tqdm(ds, total=len(ds), leave=False):
        sizes.append(data.size()[1:])
    sizes = np.array(sizes)
    print(
        "data resolution stats: \n\tmin: {:s}\n\tmax: {:s}\n\tmean: {:s}\n\tmedian: {:s}".format(
            str(np.min(sizes, axis=0)),
            str(np.max(sizes, axis=0)),
            str(np.mean(sizes, axis=0)),
            str(np.median(sizes, axis=0)),
        )
    )

    ds = PPPP(train=False)

    L = len(ds)
    print("length test set: {:d}".format(L))
    img, label = ds[1]
    img.show()

    tf = transforms.Compose(
        [
            transforms.Resize(224),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
        ]
    )
    ds = PPPP(train=True, transform=tf)

    ds.__compute_mean_std__()
    L = len(ds)
    print("length train set: {:d}".format(L))

    from matplotlib import pyplot as plt

    ds = PPPP()
    hist = ds.labels.hist(bins=3, column="Numeric_Label")
    plt.show()
