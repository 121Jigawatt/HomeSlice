import { createContext, useContext } from "react";
import { AuthStatus } from "./api";

export type Permission =
  | "library.view"
  | "library.upload"
  | "library.delete"
  | "slice.run"
  | "jobs.view_all"
  | "jobs.manage_all"
  | "printers.view"
  | "printers.print"
  | "printers.queue"
  | "printers.manage"
  | "orca.desktop"
  | "orca.manage"
  | "activity.view"
  | "users.manage";

export interface Me {
  username: string;
  can: (...anyOf: Permission[]) => boolean;
  /** True for something this user created (models, jobs, queued prints). */
  owns: (owner: string | null | undefined) => boolean;
}

export function makeMe(status: AuthStatus): Me {
  const perms = new Set(status.permissions);
  const username = status.username ?? "";
  return {
    username,
    can: (...anyOf) => anyOf.some((p) => perms.has(p)),
    owns: (owner) => !!owner && owner.toLowerCase() === username.toLowerCase(),
  };
}

export const AuthContext = createContext<Me>({ username: "", can: () => false, owns: () => false });

export function useMe(): Me {
  return useContext(AuthContext);
}
