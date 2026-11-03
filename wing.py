import numpy as np

class Wing:
    def __init__(
        self,
        span,
        root_chord,
        taper_ratio,
        sweep_deg,
        dihedral_deg
    ):
        self.span = span
        self.root_chord = root_chord
        self.taper_ratio = taper_ratio
        self.sweep_deg = sweep_deg
        self.dihedral_deg = dihedral_deg

    def calculate_geometry(self):
        half_span = self.span / 2

        tip_chord = (
            self.root_chord * self.taper_ratio
        )

        tip_x = (
            half_span *
            np.tan(np.radians(self.sweep_deg))
        )

        tip_z = (
            half_span *
            np.tan(np.radians(self.dihedral_deg))
        )

        area = (
            self.span *
            (self.root_chord + tip_chord) / 2
        )

        return {
            "tip_chord": tip_chord,
            "tip_x": tip_x,
            "tip_z": tip_z,
            "wing_area": area
        }


wing = Wing(
    span=10,
    root_chord=1.5,
    taper_ratio=0.5,
    sweep_deg=15,
    dihedral_deg=5
)

print(wing.calculate_geometry())