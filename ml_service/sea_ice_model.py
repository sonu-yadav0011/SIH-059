import numpy as np
import tensorflow as tf
from pathlib import Path


# Paths
BASE_DIR = Path(__file__).resolve().parent

MODEL_PATH = (
    BASE_DIR /
    "models" /
    "seasonal_cnn.keras"
)

ARTIFACT_DIR = (
    BASE_DIR /
    "artifacts"
)

# Load model and artifacts
model = tf.keras.models.load_model(
    MODEL_PATH,
    compile=False
)
spatial_mask = np.load(
    ARTIFACT_DIR /
    "spatial_mask.npy"
)
latitude_grid = np.load(
    ARTIFACT_DIR /
    "latitude_grid.npy"
)

longitude_grid = np.load(
    ARTIFACT_DIR /
    "longitude_grid.npy"
)

# Seasonal features
def create_seasonal_features(
    target_date,
    height,
    width
):

    target_date = np.datetime64(
        target_date
    )

    year_start = target_date.astype(
        "datetime64[Y]"
    )

    day_of_year = int(
        (
            target_date - year_start
        )
        .astype("timedelta64[D]")
        .astype(int)
    ) + 1

    angle = (
        2
        * np.pi
        * (day_of_year - 1)
        / 365.25
    )

    sin_feature = np.full(
        (height, width),
        np.sin(angle),
        dtype=np.float32
    )

    cos_feature = np.full(
        (height, width),
        np.cos(angle),
        dtype=np.float32
    )

    return (
        sin_feature,
        cos_feature
    )


# Prepare model input

def prepare_model_input(
    last_7_days,
    target_date
):

    last_7_days = np.asarray(
        last_7_days,
        dtype=np.float32
    )

    expected_shape = (
        7,
        66,
        57
    )

    if last_7_days.shape != expected_shape:

        raise ValueError(
            f"Expected shape "
            f"{expected_shape}, "
            f"received "
            f"{last_7_days.shape}"
        )


    # Replace NaN values
    ice_data = np.nan_to_num(
        last_7_days,
        nan=0.0
    )


    # Create seasonal channels
    sin_feature, cos_feature = (
        create_seasonal_features(
            target_date,
            66,
            57
        )
    )


    # (7, 66, 57)
    # →
    # (66, 57, 7)

    ice_channels = np.transpose(
        ice_data,
        (1, 2, 0)
    )


    # Add seasonal channels

    model_input = np.concatenate(
        [
            ice_channels,
            sin_feature[..., np.newaxis],
            cos_feature[..., np.newaxis]
        ],
        axis=-1
    )


    # Add batch dimension

    model_input = model_input[
        np.newaxis,
        ...
    ]


    return model_input.astype(
        np.float32
    )


# Main prediction function
def predict_sea_ice(
    last_7_days,
    target_date
):

    # Prepare input
    model_input = prepare_model_input(
        last_7_days,
        target_date
    )


    # Run CNN
    prediction = model.predict(
        model_input,
        verbose=0
    )[0]


    # Apply mask
    prediction = np.where(
        spatial_mask,
        prediction,
        np.nan
    )


    # Safety clipping
    prediction = np.clip(
        prediction,
        0.0,
        1.0
    )


    return prediction